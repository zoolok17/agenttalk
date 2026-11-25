"""The store fence: agenttalk refuses every message store outside AGENTTALK_STORE_FENCE,
and the test suite fences itself to pytest's temporary folder."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from _pytest.tmpdir import get_user

import agenttalk
from agenttalk import cli
from agenttalk.store import Store

SRC = Path(agenttalk.__file__).resolve().parents[1]   # the agenttalk under test, also in subprocesses
TESTS = Path(__file__).resolve().parent


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _dir_link(link: Path, target: Path) -> None:
    """A folder link that needs no administrator: a junction on Windows, a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _unlink_dir(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)            # removes the junction itself, never what it points to
    else:
        os.unlink(link)


@pytest.fixture
def fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """(fence, an outside store, this test's own refusal report): the test probes on purpose."""
    outside = tmp_path / "outside"
    Store(outside).init(["lead", "worker"])
    fence = tmp_path / "fence"
    fence.mkdir()
    report = tmp_path / "own-report.txt"
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))
    return fence, outside, report


def _set_role_in(root: str, rc_file: str) -> None:
    """Run in a multiprocessing child: try to change the store at `root`, keep the exit code."""
    Path(rc_file).write_text(str(cli.main(["--root", root, "roster", "set-role", "worker", "reviewer"])),
                             encoding="utf-8")


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    env.pop("AGENTTALK_ROOT", None)
    env.pop("AGENTTALK_SELF", None)
    return env


def test_the_suite_fences_every_store_to_pytests_temporary_folder(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    basetemp = tmp_path_factory.getbasetemp().resolve()
    assert Path(os.environ["AGENTTALK_STORE_FENCE"]) == basetemp
    assert Path(os.environ["AGENTTALK_STORE_FENCE_REPORT"]).parent == basetemp


def test_a_store_outside_the_fence_is_refused_however_it_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """The incident's shape: a real-looking live store, reached by a flag, by the
    inherited AGENTTALK_ROOT, by the walk up from a folder inside it, and by a
    subprocess. Every way is refused, and the store is left byte for byte as it was."""
    live = tmp_path / "live"
    Store(live).init(["lead", "worker"])
    (live / "sub").mkdir()
    fence = tmp_path / "fence"
    fence.mkdir()
    report = tmp_path / "own-report.txt"
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))   # this test probes on purpose
    before = _snapshot(live)

    from agenttalk.store import StoreFenceError

    with pytest.raises(StoreFenceError):
        Store(live)
    assert cli.main(["--root", str(live), "send", "--from", "lead", "--to", "worker", "-m", "x"]) == 2
    monkeypatch.setenv("AGENTTALK_ROOT", str(live))
    assert cli.main(["send", "--from", "lead", "--to", "worker", "-m", "x"]) == 2
    monkeypatch.delenv("AGENTTALK_ROOT")
    monkeypatch.chdir(live / "sub")
    assert cli.main(["send", "--from", "lead", "--to", "worker", "-m", "x"]) == 2
    assert "AGENTTALK_STORE_FENCE" in capsys.readouterr().err

    child = subprocess.run(
        [sys.executable, "-B", "-m", "agenttalk", "--root", str(live),
         "send", "--from", "lead", "--to", "worker", "-m", "x"],
        cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=120,
    )
    assert child.returncode == 2 and "AGENTTALK_STORE_FENCE" in child.stderr

    assert _snapshot(live) == before
    assert report.read_text(encoding="utf-8").splitlines() == [str(live.resolve())] * 5

    inside = fence / "project"
    Store(inside).init(["lead", "worker"])            # a store inside the fence works as before
    monkeypatch.chdir(inside)
    assert cli.main(["send", "--from", "lead", "--to", "worker", "-m", "hello"]) == 0


def test_a_test_that_reaches_outside_its_folder_fails_even_through_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suite's own guard, run on a small suite of its own: one test opens a store
    outside its temporary folder and swallows the error, another starts a subprocess that
    does and expects the refusal. Both fail; the test that stays inside passes. The inner
    run adds both refusals to the report it was given, this test's own."""
    outside = tmp_path / "outside"
    Store(outside).init(["lead", "worker"])
    before = _snapshot(outside)
    report = tmp_path / "own-report.txt"
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (inner / "conftest.py").write_text(_FIXTURES_CONFTEST, encoding="utf-8")
    (inner / "test_inner.py").write_text(textwrap.dedent(f"""
        import subprocess, sys
        from agenttalk.store import Store

        OUTSIDE = {str(outside)!r}

        def test_swallows_the_refusal():
            try:
                Store(OUTSIDE)
            except ValueError:
                pass

        def test_a_subprocess_reaches_outside():
            child = subprocess.run([sys.executable, "-B", "-m", "agenttalk", "--root", OUTSIDE, "roster"],
                                   capture_output=True, text=True, timeout=120)
            assert child.returncode == 2

        def test_stays_inside(tmp_path):
            Store(tmp_path / "mine").init(["lead"])
    """), encoding="utf-8")
    run = _nested_pytest(inner, "--basetemp", str(tmp_path / "inner-bt"))
    out = run.stdout + run.stderr
    assert run.returncode == 1, out
    assert "ERROR test_inner.py::test_swallows_the_refusal" in out, out
    assert "ERROR test_inner.py::test_a_subprocess_reaches_outside" in out, out
    assert "ERROR test_inner.py::test_stays_inside" not in out and "2 errors" in out, out
    assert _snapshot(outside) == before
    assert report.read_text(encoding="utf-8").splitlines() == [str(outside.resolve())] * 2


def test_a_store_whose_state_folder_links_outside_is_refused(fenced, capsys: pytest.CaptureFixture) -> None:
    """Finding 1: a project inside the fence whose .agenttalk folder, or a folder inside it,
    is a link to an outside store must not read that store's roster or write its state."""
    fence, outside, report = fenced
    before = _snapshot(outside)
    whole = fence / "whole"
    whole.mkdir()
    _dir_link(whole / ".agenttalk", outside / ".agenttalk")
    part = fence / "part"
    Store(part).init(["lead", "worker"])
    (outside / "elsewhere").mkdir()
    shutil.rmtree(part / ".agenttalk" / "state")
    _dir_link(part / ".agenttalk" / "state", outside / "elsewhere")
    try:
        from agenttalk.store import StoreFenceError

        for project in (whole, part):
            with pytest.raises(StoreFenceError):
                Store(project)
            assert cli.main(["--root", str(project), "roster", "set-role", "worker", "reviewer"]) == 2
        assert "AGENTTALK_STORE_FENCE" in capsys.readouterr().err
        assert _snapshot(outside) == before and not any((outside / "elsewhere").iterdir())
        assert len(report.read_text(encoding="utf-8").splitlines()) == 4
    finally:
        _unlink_dir(whole / ".agenttalk")
        _unlink_dir(part / ".agenttalk" / "state")


_HOOK_CONFTEST = (
    "from _store_fence import (  # noqa: F401\n"
    "    _no_store_outside_the_test_folder, _store_fence, pytest_configure)\n"
    "\n\n"
    "def pytest_sessionfinish(session, exitstatus):   # a hook of its own, as the main conftest has\n"
    "    pass\n")
_FIXTURES_CONFTEST = "from _store_fence import _no_store_outside_the_test_folder, _store_fence  # noqa: F401\n"


def _nested_pytest(inner: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    """Run the suite in `inner` as a test run of its own, started from this test."""
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "test_inner.py", "-q", "-p", "no:cacheprovider",
         "-c", "pytest.ini", "--rootdir", str(inner), "--confcutdir", str(inner), *args],
        cwd=inner, env=_env(PYTHONPATH=os.pathsep.join([str(SRC), str(TESTS)]), **env),
        capture_output=True, text=True, timeout=300,
    )


def _inner_suite(tmp_path: Path, body: str, *args: str, **env: str) -> tuple[subprocess.CompletedProcess, bool]:
    """Run `body` as a suite of its own under this guard, with pytest's `args` and extra `env`;
    (the run, outside store unchanged)."""
    outside = tmp_path / "outside"
    Store(outside).init(["lead", "worker"])
    before = _snapshot(outside)
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (inner / "conftest.py").write_text(_HOOK_CONFTEST, encoding="utf-8")
    (inner / "test_inner.py").write_text(
        "import pytest\nfrom agenttalk.store import Store\n"
        f"OUTSIDE = {str(outside)!r}\n" + textwrap.dedent(body), encoding="utf-8")
    run = _nested_pytest(inner, "--basetemp", str(tmp_path / "inner-bt"), *args, **env)
    return run, _snapshot(outside) == before


@pytest.mark.parametrize("body", [
    pytest.param("""
        try:                                   # while the module is collected
            Store(OUTSIDE).set_role("worker", "reviewer")
        except ValueError:
            pass

        def test_nothing():
            pass
    """, id="collection"),
    pytest.param("""
        @pytest.fixture(scope="session", autouse=True)
        def opened_before_any_test():
            try:
                Store(OUTSIDE).set_role("worker", "reviewer")
            except ValueError:
                pass

        def test_nothing():
            pass
    """, id="session-fixture-setup"),
    pytest.param("""
        @pytest.fixture(scope="session", autouse=True)
        def opened_at_session_end():
            yield
            try:
                Store(OUTSIDE).set_role("worker", "reviewer")
            except ValueError:
                pass

        def test_nothing():
            pass
    """, id="session-fixture-teardown"),
])
def test_a_refusal_outside_any_test_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str,
) -> None:
    """Finding 2: the fence is in place while modules are collected, and a refusal that no
    test saw (collection, a session fixture's setup or its teardown) fails the whole run.
    The inner run adds it to the report it was given, this test's own."""
    report = tmp_path / "own-report.txt"
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))
    run, unchanged = _inner_suite(tmp_path, body)
    out = run.stdout + run.stderr
    assert unchanged, out
    assert run.returncode != 0 and "this run reached a message store outside" in out, out
    assert report.read_text(encoding="utf-8").splitlines() == [str((tmp_path / "outside").resolve())]


def _everything(folder: Path, *skipped: Path) -> dict[str, bytes | None]:
    """Every folder and file below `folder` but `skipped`, with each file's bytes."""
    return {str(p.relative_to(folder)): p.read_bytes() if p.is_file() else None
            for p in sorted(folder.rglob("*")) if not any(p == s or s in p.parents for s in skipped)}


def _store_suite(tmp_path: Path, conftest: str = _HOOK_CONFTEST) -> Path:
    """A suite of its own whose one test makes a store in the folder its run is fenced to
    (not in tmp_path, which pytest itself refuses to make in some linked layouts)."""
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (inner / "conftest.py").write_text(conftest, encoding="utf-8")
    (inner / "test_inner.py").write_text(
        "import os\nfrom pathlib import Path\n\nfrom agenttalk.store import Store\n\n\n"
        "def test_a_store_in_the_folder_this_run_is_fenced_to():\n"
        "    Store(Path(os.environ['AGENTTALK_STORE_FENCE']) / 'project').init(['lead'])\n", encoding="utf-8")
    return inner


@pytest.mark.parametrize(("where", "conftest"), [
    pytest.param("outside", _HOOK_CONFTEST, id="outside"),
    pytest.param("outside", _FIXTURES_CONFTEST, id="outside-fixtures-only"),
    pytest.param("link", _HOOK_CONFTEST, id="through-a-link-to-outside"),
    pytest.param("fence", _HOOK_CONFTEST, id="the-fence-itself"),
    pytest.param("report", _HOOK_CONFTEST, id="holding-the-report"),
    pytest.param(None, _HOOK_CONFTEST, id="pytest-default-outside"),
    pytest.param("default-link", _HOOK_CONFTEST, id="pytest-default-through-a-link-to-outside"),
])
def test_a_nested_run_stops_before_it_makes_a_folder_outside_its_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str | None, conftest: str,
) -> None:
    """Round 8: a test run started from a test keeps inside the fence it was started under.
    A basetemp outside it (also through a link), the fence itself, or one that holds the
    report it was given (or, without --basetemp, a temporary root outside it) stops the run
    before pytest makes or empties anything, and the report it was given gets one line.
    Round 9: also when, without --basetemp, the root is inside but pytest's own
    pytest-of-<user> folder there is a link to outside."""
    fence = tmp_path / "fence"
    fence.mkdir()
    report = fence / "kept" / "report.txt" if where == "report" else tmp_path / "own-report.txt"
    report.parent.mkdir(exist_ok=True)
    report.write_text("an earlier refusal\n", encoding="utf-8")
    links = {"link": fence / "link", "default-link": fence / f"pytest-of-{get_user() or 'unknown'}"}
    if where in links:
        (tmp_path / "outside-target").mkdir()
        _dir_link(links[where], tmp_path / "outside-target")
    basetemp = {"outside": tmp_path / "outside-bt", "link": fence / "link" / "bt", "fence": fence,
                "report": report.parent, "default-link": None, None: None}[where]
    # where pytest makes its own folder without --basetemp
    temproot = fence if where == "default-link" else tmp_path / "outside-root"
    temproot.mkdir(exist_ok=True)
    if basetemp is not None:
        basetemp.mkdir(exist_ok=True)
        (basetemp / "kept.txt").write_text("not emptied", encoding="utf-8")
    inner = _store_suite(tmp_path, conftest)
    before = _everything(tmp_path, inner, report)
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))
    try:
        run = _nested_pytest(inner, *(["--basetemp", str(basetemp)] if basetemp else []),
                             PYTEST_DEBUG_TEMPROOT=str(temproot))

        out = run.stdout + run.stderr
        assert run.returncode == pytest.ExitCode.USAGE_ERROR, out
        assert "store fence: this test run was started under AGENTTALK_STORE_FENCE" in out, out
        assert _everything(tmp_path, inner, report) == before          # nothing made, nothing emptied
        reason = ("holds the report of the run that started this one" if where == "report"
                  else f"is not inside the fence {fence.resolve()}")
        named = tmp_path / "outside-target" if where == "default-link" else basetemp or temproot
        assert report.read_text(encoding="utf-8").splitlines() == [
            "an earlier refusal", f"{named.resolve()} (a test run's temporary folder that {reason})"]
    finally:
        if where in links:
            _unlink_dir(links[where])


@pytest.mark.parametrize("given", [True, False], ids=["basetemp-inside", "pytest-default-inside"])
def test_a_nested_run_inside_its_fence_runs_fenced_to_its_own_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: bool,
) -> None:
    """Round 8: a nested run whose basetemp (or, without --basetemp, whose temporary root)
    lies inside the fence it was started under runs, and its store stays inside."""
    fence = tmp_path / "fence"
    fence.mkdir()
    report = tmp_path / "own-report.txt"
    inner = _store_suite(tmp_path)
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))

    run = _nested_pytest(inner, *(["--basetemp", str(fence / "bt")] if given else []),
                         PYTEST_DEBUG_TEMPROOT=str(fence))

    out = run.stdout + run.stderr
    assert run.returncode == 0 and "1 passed" in out, out
    assert len(list(fence.rglob("project/.agenttalk"))) == 1, out
    assert not report.exists(), out


@pytest.mark.parametrize("name", ["user", "unknown"])
def test_both_default_folders_pytest_may_use_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    """Round 9: without --basetemp pytest uses pytest-of-<user> under its temporary root, or
    pytest-of-unknown when it cannot make that one. Either one linked outside stops the run."""
    import _store_fence

    fence, target, report = tmp_path / "fence", tmp_path / "outside-target", tmp_path / "own-report.txt"
    fence.mkdir()
    target.mkdir()
    link = fence / f"pytest-of-{(get_user() or 'unknown') if name == 'user' else 'unknown'}"
    _dir_link(link, target)
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(fence))
    try:
        with pytest.raises(pytest.exit.Exception):
            _store_fence._keep_inside(SimpleNamespace(option=SimpleNamespace(basetemp=None)), fence.resolve(),
                                      str(report))
    finally:
        _unlink_dir(link)
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"{target.resolve()} (a test run's temporary folder that is not inside the fence {fence.resolve()})"]


def test_a_basetemp_made_outside_the_fence_never_becomes_the_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9: whatever folder pytest made, a run started under a fence checks it again,
    resolved, before it becomes the fence. One outside stops the run with a report line, and
    the fence and report this process had stay as they were."""
    import _store_fence

    fence, made, report = tmp_path / "fence", tmp_path / "made-outside", tmp_path / "own-report.txt"
    fence.mkdir()
    made.mkdir()
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(fence))         # the folders checked first are inside
    monkeypatch.setattr(_store_fence, "_STATE", {})
    config = SimpleNamespace(option=SimpleNamespace(basetemp=None),
                             _tmp_path_factory=SimpleNamespace(getbasetemp=lambda: made))

    with pytest.raises(pytest.exit.Exception):
        _store_fence._install(config)

    assert _store_fence._STATE == {}
    assert os.environ["AGENTTALK_STORE_FENCE"] == str(fence)
    assert os.environ["AGENTTALK_STORE_FENCE_REPORT"] == str(report)
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"{made.resolve()} (a test run's temporary folder that is not inside the fence {fence.resolve()})"]


def test_an_xdist_run_started_under_a_fence_passes_each_refusal_on_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8: with xdist, each worker keeps inside the fence its controller set, and a
    worker's refusal reaches the report the run was given once, through the controller.
    Round 11: the inner run loads xdist the way the dev gate does (plugin autoload off,
    ``-p xdist.plugin``), so it runs whether or not the outer run autoloads plugins."""
    pytest.importorskip("xdist")
    report = tmp_path / "own-report.txt"
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(report))
    run, unchanged = _inner_suite(tmp_path, """
        def test_swallows_the_refusal():
            try:
                Store(OUTSIDE)
            except ValueError:
                pass

        def test_stays_inside(tmp_path):
            Store(tmp_path / "mine").init(["lead"])
    """, "-p", "xdist.plugin", "-n", "2", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    out = run.stdout + run.stderr
    assert unchanged, out
    assert run.returncode == 1 and "2 passed, 1 error" in out, out
    assert report.read_text(encoding="utf-8").splitlines() == [str((tmp_path / "outside").resolve())]


def test_this_suite_has_the_end_of_run_check_beside_its_own_hook(request, monkeypatch) -> None:
    """This suite's conftest defines pytest_sessionfinish for its document guard. The fence's
    check is a plugin of its own, so this run has it too; the inner suites above prove the
    same with a conftest hook of the same name."""
    import _store_fence

    plugin = request.config.pluginmanager.get_plugin(_store_fence.RUN_CHECK_PLUGIN)
    checked = []
    monkeypatch.setattr(_store_fence, "fail_run_on_refusals", checked.append)

    plugin.pytest_sessionfinish(session="this session")

    assert checked == ["this session"]


def test_a_child_with_an_environment_of_its_own_is_still_fenced(fenced, tmp_path: Path) -> None:
    """Finding 3: a child started with an explicit minimal environment, as the gateway tests'
    bare_environment builds it, still gets the fence and the report."""
    from gateway_binding_fixtures import bare_environment

    fence, outside, report = fenced
    before = _snapshot(outside)
    child = subprocess.run(
        [sys.executable, "-B", "-m", "agenttalk", "--root", str(outside), "roster", "set-role", "worker", "reviewer"],
        env=bare_environment(tmp_path / "home", localappdata=True), cwd=fence,
        capture_output=True, text=True, timeout=120,
    )
    assert child.returncode == 2 and "AGENTTALK_STORE_FENCE" in child.stderr, child.stdout + child.stderr
    assert _snapshot(outside) == before
    assert report.read_text(encoding="utf-8").splitlines() == [str(outside.resolve())]


def _set_scratch(store_root: Path, **scratch: str) -> None:
    config = store_root / ".agenttalk" / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["scratch"] = scratch
    config.write_text(json.dumps(data), encoding="utf-8")


def test_scratch_root_never_reads_or_creates_outside_the_fence(fenced) -> None:
    """Finding 4: `scratch root` read an outside store's config and created a folder there."""
    fence, outside, report = fenced
    _set_scratch(outside, root=str(outside / "scratch"))
    inside = fence / "project"
    Store(inside).init(["lead"])
    _set_scratch(inside, root=str(outside / "from-inside"))
    before = _snapshot(outside)
    assert cli.main(["--root", str(outside), "scratch", "root", "--for", "probe"]) == 2
    assert cli.main(["--root", str(inside), "scratch", "root", "--for", "probe"]) == 2
    assert cli.main(["--root", str(inside), "scratch", "store", "--for", "probe"]) == 2
    assert _snapshot(outside) == before
    assert not (outside / "scratch").exists() and not (outside / "from-inside").exists()
    refused = report.read_text(encoding="utf-8").splitlines()
    probe = outside.resolve() / "from-inside" / "probe"
    assert refused[:2] == [str(outside.resolve()), str(probe)]      # the store, before its config is read
    assert len(refused) == 3 and refused[2].startswith(str(probe / "store-"))


def test_janitor_apply_removes_and_commits_nothing_outside_the_fence(fenced, tmp_path: Path) -> None:
    """Finding 4: `janitor --apply --root OUTSIDE` removed a file outside the fence. Nothing
    is removed or committed unless the project, its scratch and temp folders and every
    registered worktree are inside the fence."""
    fence, outside, report = fenced
    # Every folder a janitor could touch is in this test's own tmp_path, even if the fence failed.
    _set_scratch(outside, root=str(tmp_path / "outside-scratch"), tmp_root=str(tmp_path / "outside-tmp"))
    (outside / ".review-sentinel.md").write_text("kept", encoding="utf-8")
    assert cli.main(["--root", str(outside), "janitor", "--apply"]) == 2

    old_tmp = tmp_path / "outside-tmp" / "agenttalk-probe-old"
    old_tmp.mkdir(parents=True)
    os.utime(old_tmp, (1, 1))
    inside = fence / "project"
    Store(inside).init(["lead"])
    _set_scratch(inside, root=str(fence / "scratch"), tmp_root=str(old_tmp.parent))
    assert cli.main(["--root", str(inside), "janitor", "--apply"]) == 2
    assert old_tmp.is_dir() and (outside / ".review-sentinel.md").read_text(encoding="utf-8") == "kept"
    assert report.read_text(encoding="utf-8").splitlines() == [
        str(outside.resolve()),                     # the store, before its config is read
        str(old_tmp.parent.resolve()),              # its temp folder, before anything is removed
    ]

    if shutil.which("git") is None:
        return
    _set_scratch(inside, root=str(fence / "scratch"), tmp_root=str(fence / "tmp"))
    git = ["git", "-C", str(inside), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for step in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "seed"],
                 ["worktree", "add", "-q", "-b", "wip", str(tmp_path / "outside-wt")]):
        subprocess.run([*git, *step], check=True, capture_output=True, timeout=60)
    (tmp_path / "outside-wt" / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    worktree_before = _snapshot(tmp_path / "outside-wt")
    assert cli.main(["--root", str(inside), "janitor", "--apply"]) == 2
    assert _snapshot(tmp_path / "outside-wt") == worktree_before
    assert report.read_text(encoding="utf-8").splitlines()[-1] == str((tmp_path / "outside-wt").resolve())


def test_a_refused_checkpoint_hook_writes_no_error_log_into_the_refused_store(fenced) -> None:
    """Finding 4: the hook's error handler created OUTSIDE/.agenttalk/checkpoints/checkpoint-errors.log."""
    fence, outside, report = fenced
    before = _snapshot(outside)
    assert cli.main(["--root", str(outside), "checkpoint", "save", "--for", "lead", "--hook"]) == 0  # fail-soft
    assert _snapshot(outside) == before and not (outside / ".agenttalk" / "checkpoints").exists()
    assert str(outside.resolve()) in report.read_text(encoding="utf-8")


def test_comprehension_and_assurance_refuse_an_outside_store(fenced) -> None:
    """The other two commands that read or write .agenttalk/ without opening the store."""
    from agenttalk import assurance

    fence, outside, report = fenced
    before = _snapshot(outside)
    assert cli.main(["--root", str(outside), "comprehension", "status"]) == 2
    assert assurance.main(["--root", str(outside)]) == 2
    inside = fence / "project"
    inside.mkdir()
    assert assurance.main(["--root", str(inside), "--out", str(outside / "runs")]) == 2
    assert _snapshot(outside) == before and not (outside / "runs").exists()
    refused = report.read_text(encoding="utf-8").splitlines()
    assert refused[:2] == [str(outside.resolve()), str(outside.resolve())]
    assert len(refused) == 3 and refused[2].startswith(str((outside / "runs").resolve()))


def test_a_state_folder_that_cannot_be_listed_is_refused(fenced, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 2, finding 1: a .agenttalk folder whose listing is denied, holding a state
    junction to an outside store, let write_heartbeat write outside. A folder that cannot be
    listed now refuses the store; only a really absent path counts as absent."""
    fence, outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead", "worker"])
    shutil.rmtree(inside / ".agenttalk" / "state")
    _dir_link(inside / ".agenttalk" / "state", outside / ".agenttalk" / "state")
    before = _snapshot(outside)
    real_scandir = os.scandir

    def denied(path):
        if Path(path) == inside / ".agenttalk":
            raise PermissionError("listing denied for this test")
        return real_scandir(path)

    try:
        from agenttalk.store import StoreFenceError

        monkeypatch.setattr(os, "scandir", denied)
        with pytest.raises(StoreFenceError, match="cannot be inspected"):
            Store(inside).write_heartbeat("worker")
        monkeypatch.undo()
        assert _snapshot(outside) == before
        assert report.read_text(encoding="utf-8").splitlines() == [
            f"{inside.resolve()} -> {inside.resolve() / '.agenttalk'} (cannot be inspected)"]
        Store(fence / "fresh")                          # a store not made yet is absent, not refused
    finally:
        _unlink_dir(inside / ".agenttalk" / "state")


@pytest.mark.parametrize("form", ["positional", "empty-fence", "other-fence", "inherited-after-lift"])
def test_a_child_cannot_drop_or_change_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str,
) -> None:
    """Round 2, finding 2: a child given an empty fence, or an environment in Popen's
    positional slot, changed an outside store. Every child now gets this run's fence and
    report, however its environment is passed, and even after the test lifted its own."""
    import _store_fence

    fence, outside, report = fenced
    before = _snapshot(outside)
    cmd = [sys.executable, "-B", "-m", "agenttalk", "--root", str(outside), "roster", "set-role", "worker", "reviewer"]
    bare = {k: v for k, v in os.environ.items() if not k.upper().startswith("AGENTTALK_")}
    bare["PYTHONPATH"] = str(SRC)
    pipe = subprocess.PIPE
    if form == "positional":
        child = subprocess.Popen(cmd, -1, None, None, pipe, pipe, None, True, False, str(fence), bare)
    elif form == "empty-fence":
        child = subprocess.Popen(cmd, stdout=pipe, stderr=pipe, cwd=fence, env=dict(bare, AGENTTALK_STORE_FENCE=""))
    elif form == "other-fence":
        child = subprocess.Popen(cmd, stdout=pipe, stderr=pipe, cwd=fence, env=dict(
            bare, AGENTTALK_STORE_FENCE=str(tmp_path), AGENTTALK_STORE_FENCE_REPORT=str(tmp_path / "elsewhere.txt")))
    else:
        monkeypatch.setitem(_store_fence._STATE, "basetemp", fence.resolve())   # this run's fence, for the test
        monkeypatch.delenv("AGENTTALK_STORE_FENCE")
        monkeypatch.setenv("PYTHONPATH", str(SRC))
        child = subprocess.Popen(cmd, stdout=pipe, stderr=pipe, cwd=fence)
    out, err = child.communicate(timeout=120)
    text = (out + err).decode("utf-8", "replace")
    assert child.returncode == 2 and "AGENTTALK_STORE_FENCE" in text, text
    assert _snapshot(outside) == before
    assert report.read_text(encoding="utf-8").splitlines() == [str(outside.resolve())]


def test_assurance_writes_no_summary_outside_the_fence(fenced, tmp_path: Path) -> None:
    """Round 2, finding 3: --summary ../../../../escaped.md, relative to the run folder,
    created a file outside the fence. Every output's final place is checked before any scan."""
    from agenttalk import assurance

    fence, outside, report = fenced
    inside = fence / "project"
    inside.mkdir()
    assert assurance.main(["--root", str(inside), "--out", "out", "--summary", "../../../../escaped.md"]) == 2
    assert assurance.main(["--root", str(inside), "--summary", str(tmp_path / "absolute.md")]) == 2
    # a summary inside the fence does not vouch for the run folder's own outputs
    assert assurance.main(["--root", str(inside), "--out", str(tmp_path / "runs"),
                           "--summary", str(fence / "summary.md")]) == 2
    assert not (tmp_path / "escaped.md").exists() and not (tmp_path / "absolute.md").exists()
    assert not (inside / "out").exists() and not (tmp_path / "runs").exists()   # refused before any scan
    refused = report.read_text(encoding="utf-8").splitlines()
    assert refused[:2] == [str(tmp_path.resolve() / "escaped.md"), str(tmp_path.resolve() / "absolute.md")]
    assert len(refused) == 3 and refused[2].startswith(str(tmp_path.resolve() / "runs"))


@pytest.mark.parametrize("mode", [[], ["--apply"]], ids=["report", "apply"])
@pytest.mark.parametrize("outside_part", ["scratch", "tmp", "worktree"])
def test_janitor_lists_and_asks_git_nothing_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside_part: str, mode: list[str],
) -> None:
    """Round 2, finding 4: janitor listed outside scratch and temp folders, and asked git
    about an outside worktree, before checking them, and never checked them in report mode."""
    from agenttalk import janitor

    fence, outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead"])
    places = {name: fence / name for name in ("scratch", "tmp")}
    places[outside_part] = tmp_path / f"outside-{outside_part}"
    for folder in places.values():
        folder.mkdir(parents=True, exist_ok=True)
    _set_scratch(inside, root=str(places["scratch"]), tmp_root=str(places["tmp"]))
    worktree = places.get("worktree", fence / "worktree")
    worktree.mkdir(exist_ok=True)
    listed, asked = [], []
    real_iterdir = janitor._safe_iterdir

    def listing(path):
        listed.append(Path(path))
        return real_iterdir(path)

    monkeypatch.setattr(janitor, "_safe_iterdir", listing)
    monkeypatch.setattr(janitor, "get_registered_worktrees", lambda repo: [inside, worktree])
    monkeypatch.setattr(janitor, "_registered_worktrees_listed", lambda repo: [inside, worktree])  # discovery's list
    monkeypatch.setattr(janitor, "is_dirty_worktree", lambda path: asked.append(Path(path)) or False)
    assert cli.main(["--root", str(inside), "janitor", *mode]) == 2
    reached = tmp_path / f"outside-{outside_part}"
    assert reached not in listed and reached not in asked
    assert report.read_text(encoding="utf-8").splitlines() == [str(reached.resolve())]


@pytest.mark.parametrize("route", [
    "spawnve-clean", "spawnv-after-lift", "system-after-lift", "multiprocessing-after-lift", "posix-spawn-clean",
])
def test_children_started_without_popen_are_fenced(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    """Round 3, finding 1: os.spawnve with a clean environment, and multiprocessing after the
    test lifted its own fence, changed an outside store. The os.spawn family, os.posix_spawn,
    os.system and multiprocessing now give every child this run's fence and report."""
    import _store_fence

    if route == "posix-spawn-clean" and not hasattr(os, "posix_spawn"):
        pytest.skip("os.posix_spawn exists only on POSIX")
    fence, outside, report = fenced
    before = _snapshot(outside)
    cmd = [sys.executable, "-B", "-m", "agenttalk", "--root", str(outside), "roster", "set-role", "worker", "reviewer"]
    argv = [f'"{a}"' if os.name == "nt" and " " in a else a for a in cmd]   # Windows spawn does not quote
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    if route.endswith("after-lift"):
        monkeypatch.setitem(_store_fence._STATE, "basetemp", fence.resolve())   # this run's fence, for the test
        monkeypatch.delenv("AGENTTALK_STORE_FENCE")
    clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("AGENTTALK_")}
    if route == "spawnve-clean":
        # Windows keeps a hidden variable for each drive ("=C:") that holds its working folder. os.environ does not
        # list it, and the fence starts every xdist worker with an environment copied from os.environ, so a worker has
        # none, and the C library's spawn call with an environment then sometimes crashes the worker (access
        # violation, #575). Setting the working folder to itself puts the variable back; nothing else changes.
        os.chdir(os.getcwd())
        rc = os.spawnve(os.P_WAIT, sys.executable, argv, clean)  # noqa: S606 - the route under test
    elif route == "spawnv-after-lift":
        rc = os.spawnv(os.P_WAIT, sys.executable, argv)  # noqa: S606 - the route under test
    elif route == "system-after-lift":
        status = os.system(subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd))  # noqa: S605 - the route under test
        rc = status if os.name == "nt" else os.waitstatus_to_exitcode(status)
    elif route == "multiprocessing-after-lift":
        rc_file = tmp_path / "child-rc.txt"
        child = multiprocessing.get_context("spawn").Process(target=_set_role_in, args=(str(outside), str(rc_file)))
        child.start()
        child.join(120)
        assert child.exitcode == 0, "the child itself did not finish"
        rc = int(rc_file.read_text(encoding="utf-8"))
    else:
        rc = os.waitstatus_to_exitcode(os.waitpid(os.posix_spawn(sys.executable, cmd, clean), 0)[1])  # noqa: S606
    assert rc == 2
    assert _snapshot(outside) == before
    assert report.read_text(encoding="utf-8").splitlines() == [str(outside.resolve())]


def _env_writer(tmp_path: Path) -> tuple[list[str], Path]:
    """A child's argv that writes the two settings it was started with to a JSON file, and that file."""
    seen, script = tmp_path / "child-env.json", tmp_path / "child_env.py"
    script.write_text(
        "import json, os, sys\n"
        "names = ('AGENTTALK_STORE_FENCE', 'AGENTTALK_STORE_FENCE_REPORT')\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
        "    json.dump({name: os.environ.get(name) for name in names}, fh)\n", encoding="utf-8")
    argv = [sys.executable, "-B", str(script), str(seen)]
    return [f'"{a}"' if os.name == "nt" and " " in a else a for a in argv], seen     # Windows spawn does not quote


def test_two_inheriting_launches_at_once_never_hand_a_child_an_empty_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9, automated comment 4231869872: two launches that inherit (os.spawnv, os.system,
    a multiprocessing start) changed and put back os.environ at the same time. With the
    test's fence lifted, B saved A's temporary settings, A put the lift back, and B's real
    child started with no fence, while B's own restore left the run's fence in place. Such
    launches now take turns."""
    import _store_fence

    fence, _, report = fenced
    monkeypatch.setitem(_store_fence._STATE, "basetemp", fence.resolve())   # this run's fence, for the test
    monkeypatch.delenv("AGENTTALK_STORE_FENCE")                            # the test lifted its own fence
    argv, seen = _env_writer(tmp_path)
    real_spawnv = os.spawnv.__wrapped__
    a_inside, b_launching, a_done = threading.Event(), threading.Event(), threading.Event()
    b_rc: list[int] = []

    def a_launch():                 # a slow launch: its change stays until B launches (at most a second)
        a_inside.set()
        b_launching.wait(1)

    def b_launch():
        b_launching.set()
        a_done.wait(10)             # A has put back what it changed
        return real_spawnv(os.P_WAIT, sys.executable, argv)

    def run_a():
        _store_fence._inheriting(a_launch)
        a_done.set()

    a = threading.Thread(target=run_a)
    a.start()
    assert a_inside.wait(10)
    b = threading.Thread(target=lambda: b_rc.append(_store_fence._inheriting(b_launch)))
    b.start()
    a.join(30)
    b.join(60)

    assert b_rc == [0]
    assert json.loads(seen.read_text(encoding="utf-8")) == {
        "AGENTTALK_STORE_FENCE": str(fence.resolve()), "AGENTTALK_STORE_FENCE_REPORT": str(report)}
    assert "AGENTTALK_STORE_FENCE" not in os.environ                       # the lift is back in place


class _NoChild(Exception):
    """Stops a launch before a real process is made."""


def test_a_narrower_fence_popen_captured_survives_the_posix_spawn_adapter(
    fenced, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10, automated comment 4232737685: on POSIX, Popen hands the environment it fenced
    to os.posix_spawn, whose wrapper fenced it again. If the test's narrower fence had been
    lifted in between, that second pass replaced it with this run's wider fence. Here Popen's
    own wrapper builds the environment, then the adapter os.posix_spawn is wrapped with runs on
    it after the lift, as Popen._posix_spawn would call it."""
    import _store_fence

    fence, _, _ = fenced
    seen: list[dict] = []

    def posix_spawn(path, argv, env, *, setsigdef=()):          # the shape of os.posix_spawn
        seen.append(env)
        raise _NoChild

    def execute_child(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest):
        monkeypatch.delenv("AGENTTALK_STORE_FENCE")              # the test lifts its narrower fence first
        _store_fence._env_argument(2)(posix_spawn, executable or args[0], args, env)

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute_child)
    with pytest.raises(_NoChild):
        subprocess.Popen([sys.executable, "-c", "pass"])

    assert seen[0]["AGENTTALK_STORE_FENCE"] == str(fence.resolve())


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="os.posix_spawn exists only on POSIX")
def test_a_narrower_fence_survives_popen_choosing_posix_spawn(fenced, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 10: the same path natively. Popen is made to start the child through os.posix_spawn,
    and the test's narrower fence is lifted just before that call; the real child still gets it."""
    fence, _, _ = fenced
    wrapped, used = os.posix_spawn, []

    def posix_spawn(*args, **kwargs):
        used.append(True)
        os.environ.pop("AGENTTALK_STORE_FENCE", None)            # lifted between Popen's capture and the spawn
        return wrapped(*args, **kwargs)

    monkeypatch.setattr(os, "posix_spawn", posix_spawn)
    monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", True)
    child = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('AGENTTALK_STORE_FENCE'))"],
                           stdout=subprocess.PIPE, close_fds=False, timeout=60)
    if not used:
        pytest.skip("this Python did not start the child through os.posix_spawn")
    assert child.stdout.decode().strip() == str(fence.resolve())


@pytest.mark.parametrize(("given", "kept"), [
    ("narrower", True), ("the-fence", True), ("wider", False), ("sibling", False), ("link-to-outside", False),
    ("empty", False),
])
def test_a_fence_the_child_already_holds_is_kept_only_inside_its_own(
    fenced, tmp_path: Path, given: str, kept: bool,
) -> None:
    """Round 10: an environment that already holds a fence keeps it only when it lies inside
    the fence the child would get; one that is empty, wider, beside it or a link leading
    outside is replaced, so a caller can still never widen a child's fence."""
    import _store_fence

    fence, _, _ = fenced
    target = {"narrower": fence / "inner", "the-fence": fence, "wider": tmp_path, "sibling": tmp_path / "other",
              "link-to-outside": fence / "link", "empty": None}[given]
    if given == "link-to-outside":
        (tmp_path / "other").mkdir()
        _dir_link(fence / "link", tmp_path / "other")
    seen: list[dict] = []

    def launch(path, argv, env):
        seen.append(env)
        return 0

    try:
        _store_fence._env_argument(2)(launch, "prog", ["prog"],
                                      {"AGENTTALK_STORE_FENCE": "" if target is None else str(target)})
    finally:
        if given == "link-to-outside":
            _unlink_dir(fence / "link")

    assert seen[0]["AGENTTALK_STORE_FENCE"] == str((target if kept else fence).resolve())


def test_a_popen_child_gets_its_own_environment_even_when_it_could_inherit(
    fenced, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9: Popen let a child inherit os.environ when it already held this run's
    settings, so a child made while another launch was putting a lifted fence back could
    start with none. Every Popen child now gets an environment of its own, built first."""
    fence, _, _ = fenced
    real_execute = subprocess.Popen._execute_child

    def another_launch_puts_a_lift_back(self, *args, **kwargs):    # just before the child is made
        os.environ.pop("AGENTTALK_STORE_FENCE", None)
        return real_execute(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "_execute_child", another_launch_puts_a_lift_back)
    child = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('AGENTTALK_STORE_FENCE'))"],
                           capture_output=True, text=True, timeout=60)

    assert child.stdout.strip() == str(fence.resolve()), child.stdout + child.stderr


def test_a_hard_link_in_the_state_folder_is_refused(fenced, tmp_path: Path) -> None:
    """Round 3, finding 2: a pre-made hard link inside the state folder to an outside file let
    the hook's error log append to that outside file. A file with a second name is refused."""
    from agenttalk import checkpoint
    from agenttalk.store import StoreFenceError

    fence, outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead", "worker"])
    sentinel = tmp_path / "outside-sentinel.log"
    sentinel.write_text("original\n", encoding="utf-8")
    log = inside / ".agenttalk" / "checkpoints" / "checkpoint-errors.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    os.link(sentinel, log)
    with pytest.raises(StoreFenceError, match="another name"):
        Store(inside)
    checkpoint.log_hook_error(inside, "save", ValueError("an error to log"))
    assert sentinel.read_text(encoding="utf-8") == "original\n"
    line = f"{inside.resolve()} -> {inside.resolve() / '.agenttalk' / 'checkpoints' / 'checkpoint-errors.log'}"
    assert report.read_text(encoding="utf-8").splitlines() == [line + " (has another name)"] * 2


@pytest.mark.parametrize("private", ["..acceptance-write.lock.{token}.prepare",
                                     ".{token}.unlink"])
def test_the_stores_own_lock_link_is_not_refused(fenced, private: str) -> None:
    """Round 7: while another process takes a lock, the lock file and agenttalk's private
    name for it are one file with two names, both in the state folder. CI's racing relays
    met exactly this; it is not a link to anywhere else."""
    import uuid

    fence, _outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead", "worker"])
    state = inside / ".agenttalk"
    lock = state / ".acceptance-write.lock"
    lock.write_text('{"protocol": "o_excl_v2"}', encoding="utf-8")
    os.link(lock, state / private.format(token=uuid.uuid4().hex))
    assert os.lstat(lock).st_nlink == 2

    Store(inside)

    assert not report.exists() or report.read_text(encoding="utf-8") == ""


def test_a_second_name_elsewhere_inside_the_fence_is_still_refused(fenced) -> None:
    """Only names inside the store's own state folder are accounted for; a second name in
    the rest of the fence may be anything, so the fence still cannot vouch for it."""
    from agenttalk.store import StoreFenceError

    fence, _outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead", "worker"])
    elsewhere = fence / "elsewhere.log"
    elsewhere.write_text("x", encoding="utf-8")
    os.link(elsewhere, inside / ".agenttalk" / "aliased.log")

    with pytest.raises(StoreFenceError, match="another name"):
        Store(inside)


def test_a_second_name_with_no_file_identity_is_refused(fenced, monkeypatch: pytest.MonkeyPatch) -> None:
    """Names are matched by file identity; where the system gives none (st_ino 0), two
    different files could be counted as one, so a file with a second name is refused."""
    from agenttalk import store as store_mod
    from agenttalk.store import StoreFenceError

    fence, _outside, _report = fenced
    inside = fence / "project"
    Store(inside).init(["lead", "worker"])
    state = inside / ".agenttalk"
    for name in ("first", "second"):            # each has its other name outside the fence
        outside_twin = fence.parent / f"{name}-twin.log"
        outside_twin.write_text(name, encoding="utf-8")
        os.link(outside_twin, state / f"{name}.log")
    real_lstat = os.lstat

    def without_identity(path, *args, **kwargs):
        status = real_lstat(path, *args, **kwargs)
        fields = list(status[:10])
        fields[1] = 0                            # st_ino
        return os.stat_result(fields)

    monkeypatch.setattr(store_mod.os, "lstat", without_identity)
    with pytest.raises(StoreFenceError, match="another name"):
        Store(inside)


def test_the_fence_looks_again_before_refusing_a_second_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A lock's second name lasts a few file operations, so a walk can catch one name of it
    but not the other. The fence walks again briefly before it refuses; a second name that
    is still there after those looks is refused."""
    from agenttalk import store as store_mod
    from agenttalk.store import StoreFenceError

    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(tmp_path))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(tmp_path / "report.txt"))
    linked = (tmp_path / ".agenttalk" / ".acceptance-write.lock", "linked")
    walks: list[int] = []

    def settles_on_the_second_walk(_root, _allowed):
        walks.append(1)
        return linked if len(walks) == 1 else None

    monkeypatch.setattr(store_mod, "_first_escape", settles_on_the_second_walk)
    store_mod.check_store_fence(tmp_path)
    assert len(walks) == 2

    walks.clear()
    monkeypatch.setattr(store_mod, "_first_escape", lambda _root, _allowed: walks.append(1) or linked)
    with pytest.raises(StoreFenceError, match="another name"):
        store_mod.check_store_fence(tmp_path)
    assert len(walks) == 1 + store_mod._LINK_SETTLE_TRIES


def test_a_backup_under_the_fence_leaves_the_store_usable(fenced) -> None:
    """Round 7, issue #423: a hard-link backup gave every store file a second name in its
    snapshot, so the next command refused the store. Under the fence a backup copies."""
    from agenttalk import recovery

    fence, _outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead", "worker"])
    Store(inside).send(sender="lead", recipient="worker", body="before the backup")

    result = recovery.create_backup(Store(inside), dest_root=fence / "backups")

    store = Store(inside)
    store.send(sender="lead", recipient="worker", body="after the backup")
    assert result.hardlink_used is False
    assert cli.main(["--root", str(inside), "roster", "set-role", "worker", "reviewer"]) == 0
    assert all(os.lstat(p).st_nlink == 1 for p in (inside / ".agenttalk").rglob("*") if p.is_file())
    assert not report.exists() or report.read_text(encoding="utf-8") == ""


def test_a_state_file_whose_names_cannot_be_counted_is_refused(fenced, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file in the state folder whose link count cannot be read is refused like a folder
    that cannot be listed: the fence cannot vouch for it."""
    from agenttalk.store import StoreFenceError

    fence, outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead"])
    # Resolved before os.lstat is replaced: on POSIX, resolving calls os.lstat itself.
    config = (inside / ".agenttalk" / "config.json").resolve()
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):
        if Path(path) == config:
            raise PermissionError("stat denied for this test")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    with pytest.raises(StoreFenceError, match="cannot be inspected"):
        Store(inside)
    monkeypatch.undo()
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"{inside.resolve()} -> {config} (cannot be inspected)"]


@pytest.mark.parametrize("given", ["inherited", "explicit"])
def test_posix_spawn_is_fenced_with_an_inherited_or_an_explicit_environment(
    given: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5: os.posix_spawn takes env=None for the inherited environment, and Python
    3.14's subprocess passes that on. The wrapper starts from a copy of os.environ then;
    an explicit mapping is still taken as given."""
    import _store_fence

    monkeypatch.setenv("AGENTTALK_FENCE_TEST_PARENT", "from the parent")
    seen: dict = {}

    def launch(path, argv, env, *args, **kwargs):
        seen.update(path=path, env=env, kwargs=kwargs)
        return 4242

    env = None if given == "inherited" else {"ONLY_THIS": "1"}
    assert _store_fence._env_argument(2)(launch, "/bin/true", ["true"], env, setpgroup=0) == 4242

    settings = _store_fence._child_settings()
    assert settings
    assert seen["path"] == "/bin/true" and seen["kwargs"] == {"setpgroup": 0}
    if given == "inherited":
        assert seen["env"] is not os.environ
        assert seen["env"]["AGENTTALK_FENCE_TEST_PARENT"] == "from the parent"
        assert all(seen["env"][key] == value for key, value in settings.items())
    else:
        assert seen["env"] == {"ONLY_THIS": "1", **settings}


@pytest.mark.parametrize("name", ["spawnve", "spawnvpe"])
@pytest.mark.parametrize("call", ["positional", "keyword", "mixed"])
def test_spawnve_is_fenced_however_its_arguments_are_passed(
    name: str, call: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 6: on POSIX, os.spawnve and os.spawnvpe are Python functions that also take
    their arguments by keyword. The guard binds the launcher's own signature, so every valid
    form is fenced, and a call the launcher would refuse is refused the same way."""
    import _store_fence

    seen: list = []

    def launcher(mode, file, args, env):          # os.py's own signature, without the launch
        seen.append(env)
        return 4242

    monkeypatch.setattr(os, name, launcher, raising=False)
    _store_fence._fence_other_launches()
    wrapped = getattr(os, name)
    assert wrapped is not launcher
    given = {"ONLY_THIS": "1"}
    forms = {
        "positional": lambda: wrapped(os.P_NOWAIT, "prog", ["prog"], given),
        "keyword": lambda: wrapped(mode=os.P_NOWAIT, file="prog", args=["prog"], env=given),
        "mixed": lambda: wrapped(os.P_NOWAIT, "prog", args=["prog"], env=given),
    }

    assert forms[call]() == 4242
    assert seen == [{"ONLY_THIS": "1", **_store_fence._child_settings()}]
    with pytest.raises(TypeError):
        wrapped(os.P_NOWAIT, "prog", ["prog"])     # no env: the launcher itself refuses this
    assert len(seen) == 1


def test_a_positional_only_launcher_keeps_its_own_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows' os.spawnve takes its arguments by position only; a keyword call stays refused."""
    import _store_fence

    seen: list = []

    def launcher(mode, path, argv, env, /):
        seen.append(env)
        return 1

    launch = _store_fence._env_argument(3)
    assert launch(launcher, os.P_NOWAIT, "prog", ["prog"], {"A": "1"}) == 1
    assert seen == [{"A": "1", **_store_fence._child_settings()}]
    with pytest.raises(TypeError):
        launch(launcher, mode=os.P_NOWAIT, path="prog", argv=["prog"], env={"A": "1"})
    assert len(seen) == 1


def test_a_launcher_whose_signature_cannot_be_read_is_fenced_by_position_or_keyword() -> None:
    import _store_fence

    seen: list = []

    class Launcher:
        __signature__ = "unreadable"               # inspect.signature cannot read this

        def __call__(self, *args, **kwargs):
            seen.append(kwargs["env"] if "env" in kwargs else args[3])
            return 1

    launch = _store_fence._env_argument(3)
    launch(Launcher(), os.P_NOWAIT, "prog", ["prog"], {"A": "1"})
    launch(Launcher(), os.P_NOWAIT, "prog", ["prog"], env={"A": "1"})

    assert seen == [{"A": "1", **_store_fence._child_settings()}] * 2


def test_text_and_byte_spellings_of_the_fence_become_one(fenced) -> None:
    """Round 3, finding 3: str(b'NAME') is not the name, so byte-keyed copies of the fence and
    the report survived next to the text ones. Exactly one of each now reaches the child."""
    import _store_fence

    fence, outside, report = fenced
    child = _store_fence._fenced({
        b"AGENTTALK_STORE_FENCE": b"", "agenttalk_store_fence": "",
        b"AGENTTALK_STORE_FENCE_REPORT": b"elsewhere.txt", "PATH": "kept",
    })
    names = [_store_fence._name(key) for key in child]
    assert names.count("AGENTTALK_STORE_FENCE") == 1 and names.count("AGENTTALK_STORE_FENCE_REPORT") == 1
    assert child["AGENTTALK_STORE_FENCE"] == str(fence.resolve())
    assert child["AGENTTALK_STORE_FENCE_REPORT"] == str(report) and child["PATH"] == "kept"


@pytest.mark.skipif(os.name == "nt", reason="Windows takes only text environment keys")
def test_a_posix_child_given_byte_keys_is_fenced(fenced) -> None:
    fence, outside, report = fenced
    before = _snapshot(outside)
    env = {os.fsencode(k): os.fsencode(v) for k, v in os.environ.items() if not k.startswith("AGENTTALK_")}
    env[b"AGENTTALK_STORE_FENCE"] = b""
    env[b"PYTHONPATH"] = os.fsencode(str(SRC))
    child = subprocess.run(
        [sys.executable, "-B", "-m", "agenttalk", "--root", str(outside), "roster", "set-role", "worker", "reviewer"],
        env=env, capture_output=True, text=True, timeout=120)
    assert child.returncode == 2, child.stdout + child.stderr
    assert _snapshot(outside) == before


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run([shutil.which("git"), "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _merged_checkout(fence: Path, checkout: Path, scratch: Path) -> Path:
    """A repository inside the fence, with a local origin, whose merged and clean checkout at
    `checkout` is registered: one janitor --release would remove. Its scratch folder is `scratch`."""
    repo = fence / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    for key, value in (("user.name", "Test Author"), ("user.email", "test@example.invalid"), ("gc.auto", "0")):
        _git(repo, "config", key, value)
    (repo / "source.txt").write_text("original\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".agenttalk/\n.worktrees/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "clone", "--bare", str(repo), str(fence / "remote.git"))
    _git(repo, "remote", "add", "origin", str(fence / "remote.git"))
    scratch.mkdir(parents=True, exist_ok=True)
    (fence / "temp").mkdir()
    (repo / ".agenttalk").mkdir()
    (repo / ".agenttalk" / "config.json").write_text(json.dumps(
        {"scratch": {"root": str(scratch), "tmp_root": str(fence / "temp")}}), encoding="utf-8")
    _git(repo, "worktree", "add", "-b", "finished", str(checkout))
    return repo


@pytest.mark.parametrize("layout", ["scratch-outside", "checkout-outside"])
@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_refuses_a_checkout_outside_the_fence(
    fenced, tmp_path: Path, layout: str, mode: str,
) -> None:
    """Round 12: janitor's release modes returned before the fence's folder checks, so a fenced
    --release could remove a merged checkout outside the fence, and --release-report walked it.
    Both now check the scratch, temp and .worktrees folders, every registered checkout and the
    one --release names first: the outside one is refused, and nothing is listed or removed."""
    fence, _, report = fenced
    fence = fence.resolve()
    scratch = (tmp_path / "outside-scratch").resolve() if layout == "scratch-outside" else fence / "scratch"
    checkout = scratch / "done" if layout == "scratch-outside" else (tmp_path / "outside-checkout").resolve()
    repo = _merged_checkout(fence, checkout, scratch)
    before = _snapshot(checkout)

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 2
    assert _snapshot(checkout) == before and (checkout / ".git").exists()
    assert str(checkout) in _git(repo, "worktree", "list").replace("/", os.sep)
    refused = scratch if layout == "scratch-outside" else checkout
    assert report.read_text(encoding="utf-8").splitlines() == [str(refused)]


def test_janitor_release_refuses_an_outside_path_it_was_named(fenced, tmp_path: Path) -> None:
    """Round 12: the path --release names is checked too, even when it is not a registered
    checkout (release itself would only refuse it after looking at its parent folders)."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo = _merged_checkout(fence, fence / "scratch" / "done", fence / "scratch")
    named = (tmp_path / "outside-folder").resolve()
    named.mkdir()

    assert cli.main(["--root", str(repo), "janitor", "--release", str(named)]) == 2

    assert report.read_text(encoding="utf-8").splitlines() == [str(named)]
    assert (fence / "scratch" / "done" / ".git").exists()


def _upload_packs(trace: Path) -> list[str]:
    """The lines of a GIT_TRACE file where Git started reading a remote repository."""
    return [line for line in trace.read_text(encoding="utf-8").splitlines() if "upload-pack" in line] \
        if trace.exists() else []


_UNPLACED = " (not a folder the fence can check)"


@pytest.mark.parametrize("origin", ["outside-path", "outside-relative", "outside-file-url", "insteadof-to-outside",
                                    "network"])
@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_refuses_an_origin_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str, mode: str,
) -> None:
    """Round 13: release and release-report ask origin for its default branch and fetch it. A
    local origin outside the fence was read by Git (two upload-packs) with no refusal. Now
    origin's effective address is checked first, after insteadOf and with links followed:
    only a local folder inside the fence passes, and anything else is refused before Git
    contacts it."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    outside = (tmp_path / "outside-origin.git").resolve()
    _git(repo, "clone", "--bare", str(repo), str(outside))
    url = {"outside-path": str(outside), "outside-relative": os.path.relpath(outside, repo),
           "outside-file-url": outside.as_uri(), "insteadof-to-outside": "https://example.invalid/outside-origin.git",
           "network": "git@example.invalid:origin.git"}[origin]
    _git(repo, "remote", "set-url", "origin", url)
    if origin == "insteadof-to-outside":
        _git(repo, "config", f"url.{tmp_path.resolve().as_posix()}/.insteadOf", "https://example.invalid/")
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    monkeypatch.setenv("GIT_SSH_COMMAND", "exit 1")               # never a real network, on any code

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 2
    assert _upload_packs(trace) == []
    assert (checkout / ".git").exists() and str(checkout) in _git(repo, "worktree", "list").replace("/", os.sep)
    expected = {"network": f"git remote origin, which is not a local folder{_UNPLACED}",
                "outside-relative": f"{repo / url} -> {outside}"}.get(origin, str(outside))
    assert report.read_text(encoding="utf-8").splitlines() == [expected]


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_with_an_origin_inside_the_fence_still_works(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: str,
) -> None:
    """Round 13: an origin inside the fence is asked and fetched as before."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 0, capsys.readouterr().out
    packs = _upload_packs(trace)
    assert packs and all(str(fence / "remote.git") in line for line in packs), packs
    assert not report.exists()
    if mode == "--release":
        assert not checkout.exists()
    else:
        assert f"WOULD RELEASE {checkout}" in capsys.readouterr().out


# --- #858 fix round 14: the fence checks the EXACT registered paths that the probes use ---------------------------

LINE_SEPARATOR = " "  # a legal file name character; Python's splitlines() treats it as a line break

_MODES = {"report": [], "apply": ["--apply"], "release-report": ["--release-report"],
          "release": ["--release", "{done}"]}
_JANITOR_MODES = [pytest.param(args, id=name) for name, args in _MODES.items()]
# Which list is made to fail, in which modes: the sweep's strict listing is held by all four (one preflight serves every
# mode); release's own list is read by the two release modes only.
_LIST_CASES = [
    pytest.param(which, _MODES[name], id=f"{name}-{which}")
    for which, names in (("janitor-list", list(_MODES)), ("release-list", ["release-report", "release"]))
    for name in names
]


def _outside_clone(tmp_path: Path) -> Path:
    """A plain repository OUTSIDE the fence; a `git status` there rewrites its index (a file has another time)."""
    clone = tmp_path / "outside-clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    for key, value in (("user.name", "Test Author"), ("user.email", "test@example.invalid")):
        _git(clone, "config", key, value)
    (clone / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(clone, "add", ".")
    _git(clone, "commit", "-qm", "base")
    os.utime(clone / "tracked.txt", (1_000_000_000, 1_000_000_000))  # so that status must rewrite the index
    return clone


def _janitor_args(mode: list[str], done: Path) -> list[str]:
    return [part.format(done=done) for part in mode]


def _spy_on_probes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every checkout the janitor asks `git status` about, in order."""
    from agenttalk import janitor

    asked: list[Path] = []
    real = janitor.is_dirty_worktree
    monkeypatch.setattr(janitor, "is_dirty_worktree", lambda path: asked.append(Path(path)) or real(path))
    return asked


def _linked_registered_checkout(fence: Path, tmp_path: Path, name: str) -> tuple[Path, Path, Path, Path]:
    """(repo, an ordinary in-fence checkout `done`, the registered checkout `link`, the outside clone).

    `link` is registered under `name` inside the fence, and is a link (a junction on Windows, which needs no rights; a
    symlink elsewhere) to the outside clone: Git still lists the name, and a `git status` run there would run in, and
    rewrite the index of, the clone that is outside the fence."""
    scratch = fence / "scratch"
    done = scratch / "done"
    repo = _merged_checkout(fence, done, scratch)
    link = scratch / name
    _git(repo, "worktree", "add", "-b", "lane-linked", str(link))
    shutil.rmtree(link)
    clone = _outside_clone(tmp_path)
    _dir_link(link, clone)
    return repo, done, link, clone


@pytest.mark.parametrize("mode", _JANITOR_MODES)
def test_a_registered_link_whose_name_holds_a_line_separator_is_fenced_before_any_probe(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: list[str],
) -> None:
    """The review bot's P1. The fence checked the registered checkouts as the old text reader lists them, which cuts a
    name with U+2028 short, to a path that does not exist. A registered checkout with such a name that is a link to a
    checkout outside the fence therefore passed, and the report, which probes the exact registered path, ran `git
    status` in the outside checkout and rewrote its index. The fence now checks the exact paths the probes use, so the
    link is refused first, in every mode (the report, the sweep, release-report and release), and nothing outside is
    touched: no probe is made there and its index is byte for byte the same."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo, done, link, clone = _linked_registered_checkout(fence, tmp_path, f"a{LINE_SEPARATOR}b")
    index = clone / ".git" / "index"
    before_index, before_clone = index.read_bytes(), _snapshot(clone / ".git")
    asked = _spy_on_probes(monkeypatch)
    try:
        rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])
    finally:
        _unlink_dir(link)
    outside = clone.resolve()
    assert rc == 2
    reached = [Path(os.path.realpath(path)) for path in asked]
    assert all(path != outside and outside not in path.parents for path in reached), f"a probe went outside: {asked}"
    assert index.read_bytes() == before_index and _snapshot(clone / ".git") == before_clone
    assert (done / ".git").exists() and (done / "source.txt").exists()      # nothing was released or swept
    refused = report.read_text(encoding="utf-8").split("\n")[:-1]
    assert len(refused) == 1 and refused[0].endswith(f" -> {outside}"), refused


@pytest.mark.parametrize("which, mode", _LIST_CASES)
def test_a_registered_list_that_cannot_be_read_reliably_under_the_fence_refuses(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str, mode: list[str],
) -> None:
    """With no list of exact paths that the fence can check, nothing is probed. The sweep's strict listing failing
    refuses all four modes (the release modes hold it too, so one preflight serves every mode); release's own list
    failing refuses the two release modes, which are the ones that read it."""
    from agenttalk import janitor, worktree_release

    fence, _, report = fenced
    fence = fence.resolve()
    done = fence / "scratch" / "done"
    repo = _merged_checkout(fence, done, fence / "scratch")

    def broken_janitor_list(_repo):
        raise janitor._UseCheckFailed("could not list the registered checkouts (test)")

    def broken_release_list(_repo):
        raise worktree_release.Refused("git worktree list failed (test)")

    if which == "janitor-list":
        monkeypatch.setattr(janitor, "_registered_worktrees_listed", broken_janitor_list)
    else:
        monkeypatch.setattr(worktree_release, "_registered", broken_release_list)
    asked = _spy_on_probes(monkeypatch)

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    assert rc == 2 and asked == []
    assert (done / ".git").exists() and (done / "source.txt").exists()
    lines = report.read_text(encoding="utf-8").split("\n")[:-1]
    assert len(lines) == 1 and "registered checkouts" in lines[0] and lines[0].endswith(_UNPLACED), lines


@pytest.mark.parametrize("mode", [
    pytest.param([], id="report"),
    pytest.param(["--apply"], id="apply"),
    pytest.param(["--release-report"], id="release-report"),
])
def test_ordinary_in_fence_checkouts_are_still_probed_under_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: list[str],
) -> None:
    """The control: an ordinary registered checkout inside the fence passes the preflight and is handled as before (the
    report and the sweep probe it; release-report reads it), with no refusal."""
    fence, _, report = fenced
    fence = fence.resolve()
    done = fence / "scratch" / "done"
    repo = _merged_checkout(fence, done, fence / "scratch")
    asked = _spy_on_probes(monkeypatch)

    rc = cli.main(["--root", str(repo), "janitor", *mode])

    assert rc == 0, capsys.readouterr().out
    assert not report.exists()
    if "--release-report" in mode:
        assert f"WOULD RELEASE {done}" in capsys.readouterr().out
    else:
        assert [Path(os.path.realpath(path)) for path in asked] == [Path(os.path.realpath(done))]


@pytest.mark.parametrize("fence_set", [True, False], ids=["fenced", "no-fence"])
def test_the_report_probes_only_a_strict_list_under_a_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fence_set: bool,
) -> None:
    """`build_report` can be called without the command around it. When the strict list failed it falls back to the old
    reader for the counts and the preview; under a fence it must not probe those paths (they were never the ones the
    fence checked). Without a fence the old best-effort probe is unchanged."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    done = fence / "scratch" / "done"
    repo = _merged_checkout(fence, done, fence / "scratch")
    if not fence_set:
        monkeypatch.delenv("AGENTTALK_STORE_FENCE")
    cfg = janitor.JanitorConfig.load(repo)
    monkeypatch.setattr(janitor, "_registered_worktrees_listed",
                        lambda _repo: (_ for _ in ()).throw(janitor._UseCheckFailed("could not list (test)")))
    asked = _spy_on_probes(monkeypatch)

    report = janitor.build_report(cfg)

    assert not report.registered_strict
    assert [Path(os.path.realpath(path)) for path in asked] == ([] if fence_set else [Path(os.path.realpath(done))])


@pytest.mark.parametrize("mode", [
    pytest.param(["--release-report"], id="release-report"),
    pytest.param(["--release", "{done}"], id="release"),
])
def test_a_release_fences_its_own_list_of_registered_checkouts_too(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: list[str],
) -> None:
    """A release probes the paths of ITS OWN NUL-framed list, not the sweep's. They are the same on any ordinary
    repository, so this makes release's list hold a checkout outside the fence that the sweep's list does not: the
    fence still refuses it, before release asks anything of Git."""
    from agenttalk import worktree_release

    fence, _, report = fenced
    fence = fence.resolve()
    done = fence / "scratch" / "done"
    repo = _merged_checkout(fence, done, fence / "scratch")
    stranger = (tmp_path / "outside-checkout").resolve()
    stranger.mkdir()
    real = worktree_release._registered
    monkeypatch.setattr(worktree_release, "_registered", lambda repo_: {**real(repo_), stranger: set()})

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    assert rc == 2
    assert (done / ".git").exists() and (done / "source.txt").exists()
    assert report.read_text(encoding="utf-8").split("\n")[:-1] == [str(stranger)]


# --- #858 fix round 15: every later read of the registered checkouts is fenced too ----------------------------------

_LIST_ARGS = ("worktree", "list", "--porcelain", "-z")


def _stranger_record(outside: Path) -> tuple[bytes, str]:
    """The record Git would print for a checkout registered at `outside`, as bytes (the sweep's reader) and as text."""
    sha = "0" * 40
    return (
        b"worktree " + os.fsencode(str(outside)) + b"\0HEAD " + sha.encode() + b"\0detached\0\0",
        "\0worktree " + str(outside) + "\0HEAD " + sha + "\0detached",
    )


def _register_outside_after(
    monkeypatch: pytest.MonkeyPatch, outside: Path, *, after: int = 1, only: str | None = None
) -> dict[str, int]:
    """Git's list of registered checkouts names `outside` too, from the reading number `after` + 1 on. The two readers
    (the sweep's and release's) are counted apart; the preflight makes the first reading of each; `only` limits it to
    one reader. This sits below the readers, at the output of Git, so whatever the reader itself does with a path is
    what is tested."""
    from agenttalk import janitor, worktree_release

    calls = {"sweep": 0, "release": 0}
    real_run, real_read = janitor._run_git_checked, worktree_release._read_git

    def run(repo, *args, raw=False):
        rc, out, err = real_run(repo, *args, raw=raw)
        if args[:4] == _LIST_ARGS:
            calls["sweep"] += 1
            if calls["sweep"] > after and only in (None, "sweep") and rc == 0 and isinstance(out, bytes):
                out += _stranger_record(outside)[0]
        return rc, out, err

    def read(repo, *args):
        out = real_read(repo, *args)
        if args[:4] == _LIST_ARGS:
            calls["release"] += 1
            if calls["release"] > after and only in (None, "release"):
                out += _stranger_record(outside)[1]
        return out

    monkeypatch.setattr(janitor, "_run_git_checked", run)
    monkeypatch.setattr(worktree_release, "_read_git", read)
    return calls


def _watch_looks_at(monkeypatch: pytest.MonkeyPatch, outside: Path) -> list[str]:
    """Every look at `outside` that the sweep or release makes, noted BEFORE it happens: an existence check, a
    containment or identity check, the ancestor identities of a release. (The fence's own check of a path is not one of
    these: it is the check.) The list is empty when nothing outside the fence was looked at."""
    import pathlib

    from agenttalk import janitor

    looked: list[str] = []
    target = os.path.normcase(str(outside))

    def is_outside(value) -> bool:
        return os.path.normcase(str(value)) == target

    real_exists = pathlib.Path.exists
    real_contains, real_same, real_ids = janitor._contains_worktree, janitor._same_worktree, janitor._ancestor_ids

    def exists(self, *args, **kwargs):
        if is_outside(self):
            looked.append("exists")
        return real_exists(self, *args, **kwargs)

    def contains(candidate, worktree):
        if is_outside(candidate) or is_outside(worktree):
            looked.append("containment")
        return real_contains(candidate, worktree)

    def same(left, right):
        if is_outside(left) or is_outside(right):
            looked.append("identity")
        return real_same(left, right)

    def ids(cfg):
        if is_outside(cfg.scratch_root):
            looked.append("ancestor identities")
        return real_ids(cfg)

    monkeypatch.setattr(pathlib.Path, "exists", exists)
    monkeypatch.setattr(janitor, "_contains_worktree", contains)
    monkeypatch.setattr(janitor, "_same_worktree", same)
    monkeypatch.setattr(janitor, "_ancestor_ids", ids)
    return looked


def _stale_leftover(temp: Path) -> Path:
    """A scratch-family folder in the temp root, old enough for the sweep to remove (a candidate of `--apply`)."""
    leftover = temp / "agenttalk-probe-old"
    leftover.mkdir()
    note = leftover / "note.txt"
    note.write_text("kept unless the sweep removes it", encoding="utf-8")
    for path in (note, leftover):
        os.utime(path, (1, 1))
    return leftover


_LATER_MODES = {
    "release-report": ["--release-report"],
    "release": ["--release", "{done}"],
    "apply": ["--apply"],
}


@pytest.mark.parametrize("mode", list(_LATER_MODES))
def test_a_checkout_registered_outside_after_the_preflight_is_never_looked_at(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """A later reading of Git's list is fenced by itself. After the preflight's own reading, the list also names a
    checkout outside the fence. Release read it again and passed it to its identity check (a stat of the path and its
    ancestors), and the sweep's last look asked whether it exists and whether it lies under the candidate, all before
    any fence check. Now the readers refuse the path first: nothing is looked at outside, nothing is released or swept.
    """
    fence, _, report = fenced
    fence = fence.resolve()
    scratch = fence / "scratch"
    done = scratch / "done"
    repo = _merged_checkout(fence, done, scratch)
    leftover = _stale_leftover(fence / "temp")
    stranger = (tmp_path / "outside-checkout").resolve()
    stranger.mkdir()
    calls = _register_outside_after(monkeypatch, stranger)
    looked = _watch_looks_at(monkeypatch, stranger)

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(_LATER_MODES[mode], done)])

    assert calls["sweep"] + calls["release"] >= 2, "the later reading never happened, so nothing was tested"
    assert looked == [], f"the outside checkout was looked at: {looked}"
    assert rc != 0
    assert (done / ".git").exists() and (leftover / "note.txt").exists()  # nothing was released or swept
    assert str(stranger) in report.read_text(encoding="utf-8").split("\n")


@pytest.mark.parametrize("mode", list(_LATER_MODES))
def test_the_same_run_works_when_the_later_reading_is_unchanged(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: str,
) -> None:
    """The control for the test above: no checkout is added, so the run does what it always did (the report lists, the
    release removes, the sweep removes the stale leftover), with no refusal."""
    fence, _, report = fenced
    fence = fence.resolve()
    scratch = fence / "scratch"
    done = scratch / "done"
    repo = _merged_checkout(fence, done, scratch)
    leftover = _stale_leftover(fence / "temp")

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(_LATER_MODES[mode], done)])

    assert rc == 0, capsys.readouterr().out
    assert not report.exists()
    if mode == "release":
        assert not done.exists()
    elif mode == "apply":
        assert not leftover.exists()


@pytest.mark.parametrize("mode", ["release", "apply"])
def test_a_later_reading_that_adds_an_in_fence_checkout_is_still_fence_checked_and_allowed(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """The control for the fence: a checkout registered INSIDE the fence after the preflight passes the fence, and the
    fence is asked about it (the reader checks every path it returns), so the check is not simply skipped."""
    fence, _, report = fenced
    fence = fence.resolve()
    scratch = fence / "scratch"
    done = scratch / "done"
    repo = _merged_checkout(fence, done, scratch)
    _stale_leftover(fence / "temp")
    newcomer = fence / "scratch" / "newcomer"
    newcomer.mkdir()
    _register_outside_after(monkeypatch, newcomer)  # named "outside" in the helper, but this one is inside the fence
    asked: list[Path] = []
    real_check = cli.store_mod.check_folder_fence
    monkeypatch.setattr(
        cli.store_mod, "check_folder_fence", lambda folder: asked.append(Path(folder)) or real_check(folder)
    )

    cli.main(["--root", str(repo), "janitor", *_janitor_args(_LATER_MODES[mode], done)])

    assert newcomer in asked, "the in-fence newcomer was never checked against the fence"
    assert not report.exists()


@pytest.mark.parametrize("mode", ["release-report", "release"])
def test_the_preflight_passes_on_the_fence_refusal_of_releases_own_list(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """The sweep's list is clean and only release's own list names an outside checkout from the first reading on: the
    preflight reports THAT refusal (the path, with the fence's usual line), not the generic "could not list" one."""
    fence, _, report = fenced
    fence = fence.resolve()
    done = fence / "scratch" / "done"
    repo = _merged_checkout(fence, done, fence / "scratch")
    stranger = (tmp_path / "outside-checkout").resolve()
    stranger.mkdir()
    _register_outside_after(monkeypatch, stranger, after=0, only="release")

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(_LATER_MODES[mode], done)])

    assert rc == 2 and (done / ".git").exists()
    assert report.read_text(encoding="utf-8").split("\n")[:-1] == [str(stranger)]


@pytest.mark.parametrize("reader", ["sweep", "release"])
def test_each_reader_of_the_registered_checkouts_fences_what_it_returns(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str,
) -> None:
    """One test per reader. Called directly (the preflight's own check is not in the way), the sweep's strict reader and
    release's own reader each refuse a registered path outside the fence, with the refusal report line, and return the
    path as before when no fence is set."""
    from agenttalk import janitor, worktree_release

    fence, _, report = fenced
    fence = fence.resolve()
    repo = _merged_checkout(fence, fence / "scratch" / "done", fence / "scratch")
    stranger = (tmp_path / "outside-checkout").resolve()
    stranger.mkdir()
    _register_outside_after(monkeypatch, stranger, after=0)
    read = janitor._registered_worktrees_listed if reader == "sweep" else worktree_release._registered

    with pytest.raises(cli.store_mod.StoreFenceError):
        read(repo)
    assert report.read_text(encoding="utf-8").split("\n")[:-1] == [str(stranger)]

    monkeypatch.delenv("AGENTTALK_STORE_FENCE")  # no fence: the same reading just returns it
    assert stranger in list(read(repo))


@pytest.mark.parametrize("reader", [
    "promisor-remote", "partial-clone-extension", "alternates-here", "alternates-chain", "alternates-in-origin",
    "origin-gitfile", "origin-dotgit-link", "origin-dot-git-link",
])
def test_janitor_release_refuses_other_git_reads_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str,
) -> None:
    """Round 13: the other ways Git is pointed at a repository by configuration: a partial
    clone's remote (Git fetches missing objects from it on demand), an alternates file here,
    through another one, or in origin (Git reads the object folders it names), an origin whose
    .git file names its repository elsewhere or whose .git folder is a link, and the path.git
    Git tries when origin's own path is missing. Round 15: a partial clone and an alternates
    file are refused outright rather than followed, since how Git reads them is not repeated."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    outside = (tmp_path / "outside-objects.git").resolve()
    _git(repo, "clone", "--bare", str(repo), str(outside))
    expected = str(outside / "objects")
    links: list[Path] = []
    if reader in ("promisor-remote", "partial-clone-extension"):
        _git(repo, "config", "remote.backup.url", str(outside))
        if reader == "promisor-remote":
            _git(repo, "config", "remote.backup.promisor", "true")
        else:
            _git(repo, "config", "extensions.partialClone", "backup")
        expected = f"git remote backup, which a partial clone fetches missing objects from{_UNPLACED}"
    elif reader in ("alternates-here", "alternates-chain"):
        listing = repo / ".git" / "objects" / "info" / "alternates"
        if reader == "alternates-here":
            listing.write_text(f"{outside / 'objects'}\n", encoding="utf-8")
        else:
            middle = fence / "middle.git"
            _git(repo, "clone", "--bare", str(repo), str(middle))
            (middle / "objects" / "info" / "alternates").write_text(f"{outside / 'objects'}\n", encoding="utf-8")
            listing.write_text(f"{middle / 'objects'}\n", encoding="utf-8")
        expected = f"this repository, which borrows objects through {listing}{_UNPLACED}"
    elif reader == "alternates-in-origin":
        listing = fence / "remote.git" / "objects" / "info" / "alternates"
        listing.write_text(f"{outside / 'objects'}\n", encoding="utf-8")
        expected = f"git remote origin, which borrows objects through {listing}{_UNPLACED}"
    elif reader == "origin-gitfile":
        moved = fence / "origin-checkout"
        moved.mkdir()
        (moved / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")
        _git(repo, "remote", "set-url", "origin", str(moved))
        expected = f"git remote origin: {moved / '.git'}, which names its repository elsewhere{_UNPLACED}"
    elif reader == "origin-dotgit-link":
        moved = fence / "origin-checkout"
        moved.mkdir()
        links.append(moved / ".git")
        _git(repo, "remote", "set-url", "origin", str(moved))
        expected = f"{moved / '.git'} -> {outside}"
    else:
        links.append(fence / "origin.git")                     # Git tries path.git when path is missing
        _git(repo, "remote", "set-url", "origin", str(fence / "origin"))
        expected = f"{fence / 'origin.git'} -> {outside}"
    for link in links:
        _dir_link(link, outside)
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    try:
        assert cli.main(["--root", str(repo), "janitor", "--release-report"]) == 2
    finally:
        for link in links:
            _unlink_dir(link)

    assert _upload_packs(trace) == []
    assert report.read_text(encoding="utf-8").splitlines() == [expected]


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_refuses_an_origin_git_expands_from_a_home_folder(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Round 14, automated comment 4236020031: Git expands an origin spelled ~/origin.git from
    the home folder, while the check read it as a folder inside the repository, so Git fetched
    from an outside home. Such a spelling is now refused, and release hands Git the exact
    folder it checked instead of the name origin."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    home = (tmp_path / "home").resolve()
    _git(repo, "clone", "--bare", str(repo), str(home / "origin.git"))
    _git(repo, "remote", "set-url", "origin", "~/origin.git")
    monkeypatch.setenv("HOME", str(home))                        # a throwaway home, never the real one
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 2
    assert _upload_packs(trace) == []
    assert (checkout / ".git").exists()
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"git remote origin, which is not a local folder{_UNPLACED}"]


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_never_opens_an_alternates_file_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Round 14: origin lies inside the fence, but its objects/info folder is a link to an outside
    folder holding an alternates file, which the check opened before bounding it. Each
    alternates file is now checked where it really is, links on the way followed, first."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    linked = (tmp_path / "outside-info").resolve()
    linked.mkdir()
    (linked / "alternates").write_text("# nothing listed\n", encoding="utf-8")
    info = fence / "remote.git" / "objects" / "info"
    shutil.rmtree(info)
    _dir_link(info, linked)
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    opened: list[Path] = []
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        opened.append(Path(os.path.realpath(self)))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    try:
        rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])
    finally:
        _unlink_dir(info)

    assert rc == 2
    assert [path for path in opened if path == linked or linked in path.parents] == []
    assert _upload_packs(trace) == []
    assert report.read_text(encoding="utf-8").splitlines() == [f"{info / 'alternates'} -> {linked / 'alternates'}"]


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_refuses_an_origin_that_borrows_objects_through_alternates(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Round 15, automated comment 4236195418: the check read origin's alternates entry ' alias'
    without its leading space, a name that did not exist, while Git kept the space and
    borrowed objects from the outside folder that name leads to. Under the fence an alternates
    file is no longer read at all: a repository that borrows objects is refused."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    outside = (tmp_path / "outside-objects.git").resolve()
    _git(repo, "clone", "--bare", str(repo), str(outside))
    objects = fence / "remote.git" / "objects"
    _dir_link(objects / " alias", outside / "objects")
    (objects / "info" / "alternates").write_text(" alias\n", encoding="utf-8")
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    try:
        rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])
    finally:
        _unlink_dir(objects / " alias")

    assert rc == 2
    assert _upload_packs(trace) == []
    assert (checkout / ".git").exists()
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"git remote origin, which borrows objects through {objects / 'info' / 'alternates'}{_UNPLACED}"]


def test_janitor_release_refuses_a_url_rule_that_would_rewrite_the_checked_origin(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 14: release hands Git origin's checked folder as an absolute path, and a
    url.<base>.insteadOf rule could still rewrite that path. Such a rule is refused."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo = _merged_checkout(fence, fence / "scratch" / "done", fence / "scratch")
    elsewhere = (tmp_path / "rewritten").resolve()
    _git(repo, "clone", "--bare", "--no-hardlinks", str(repo), str(elsewhere / "remote.git"))  # no shared files
    _git(repo, "remote", "set-url", "origin", os.path.relpath(fence / "remote.git", repo))
    _git(repo, "config", f"url.{elsewhere}{os.sep}.insteadOf", f"{fence}{os.sep}")
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    assert cli.main(["--root", str(repo), "janitor", "--release-report"]) == 2

    assert _upload_packs(trace) == []
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"git remote origin, whose folder Git's url settings would rewrite{_UNPLACED}"]


def test_janitor_release_checks_the_forms_git_tries_for_the_folder_it_is_handed(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 14: origin here is a link to the fence folder itself, so release hands Git the fence
    as origin's path, and Git would also try fence.git, a folder beside the fence. The forms
    Git tries are checked for the path release hands it, not only as configured."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo = _merged_checkout(fence, fence / "scratch" / "done", fence / "scratch")
    beside = Path(f"{fence}.git")
    _git(repo, "clone", "--bare", str(repo), str(beside))
    _dir_link(fence / "origin-link", fence)
    _git(repo, "remote", "set-url", "origin", str(fence / "origin-link"))
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    try:
        assert cli.main(["--root", str(repo), "janitor", "--release-report"]) == 2
    finally:
        _unlink_dir(fence / "origin-link")

    assert _upload_packs(trace) == []
    assert report.read_text(encoding="utf-8").splitlines() == [str(beside)]


def test_janitor_release_does_not_run_an_upload_pack_program_origin_names(
    fenced, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Round 14, automated comment 4236020024: origin's settings can name the program Git runs
    to read it (remote.origin.uploadpack). Release now asks origin's checked folder by path,
    so that setting no longer applies and the program never runs."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    marker = (tmp_path / "upload-pack-ran.txt").resolve()
    _git(repo, "config", "remote.origin.uploadpack", f"echo ran > '{marker.as_posix()}' #")

    rc = cli.main(["--root", str(repo), "janitor", "--release-report"])

    assert rc == 0, capsys.readouterr().out
    assert not marker.exists()
    assert f"WOULD RELEASE {checkout}" in capsys.readouterr().out
    assert not report.exists()


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_refuses_an_origin_that_shares_another_repository(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Round 16: a commondir file in origin names a repository Git takes origin's objects and
    refs from. A bare origin inside the fence whose commondir named an outside repository
    passed, and both modes fetched a commit only that outside repository held. Such an origin
    is now refused before Git runs, and the file is never read."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    common = (tmp_path / "outside-common.git").resolve()
    _git(repo, "clone", "--bare", str(fence / "remote.git"), str(common))
    tree = _git(common, "rev-parse", "HEAD^{tree}")
    only_outside = _git(common, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit-tree", tree, "-p", "HEAD", "-m", "outside only")
    _git(common, "update-ref", "refs/heads/main", only_outside)
    (fence / "remote.git" / "commondir").write_text(f"{common.as_posix()}\n", encoding="utf-8")
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 2
    assert _upload_packs(trace) == []
    assert only_outside not in _git(repo, "for-each-ref", "--format=%(objectname)")
    assert (checkout / ".git").exists()
    assert report.read_text(encoding="utf-8").splitlines() == [
        f"git remote origin: {fence / 'remote.git' / 'commondir'}, which sends Git to another folder{_UNPLACED}"]


@pytest.mark.parametrize("redirect", [
    "gitfile-path", "http-alternates", "worktrees", "linked-refs", "settings-include", "settings-promisor",
    "settings-pack-address", "worktree-settings-include",
])
def test_janitor_release_refuses_an_origin_with_anything_else_that_sends_git_elsewhere(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: str,
) -> None:
    """Round 16: the rest of what gitrepository-layout(5) and git-config(1) name as a way for a
    repository to send Git elsewhere for its objects, its refs or the repository itself: a
    file in origin's place naming its repository, an http-alternates file, a worktrees folder
    naming checkouts, a link inside the repository, and settings that read another settings
    file, fetch missing objects from another repository or hand out a pack's web address."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo = _merged_checkout(fence, fence / "scratch" / "done", fence / "scratch")
    origin = fence / "remote.git"
    outside = (tmp_path / "outside.git").resolve()
    _git(repo, "clone", "--bare", "--no-hardlinks", str(repo), str(outside))   # no shared files
    settings = f"git remote origin: {origin / 'config'}, whose settings can send Git to another folder or address"
    links: list[Path] = []
    if redirect == "gitfile-path":
        pointer = fence / "origin-pointer"
        pointer.write_text(f"gitdir: {outside}\n", encoding="utf-8")
        _git(repo, "remote", "set-url", "origin", str(pointer))
        expected = f"git remote origin: {pointer}, which names its repository elsewhere"
    elif redirect == "http-alternates":
        listing = origin / "objects" / "info" / "http-alternates"
        listing.write_text("https://example.invalid/objects\n", encoding="utf-8")
        expected = f"git remote origin, which borrows objects through {listing}"
    elif redirect == "worktrees":
        (origin / "worktrees" / "elsewhere").mkdir(parents=True)
        (origin / "worktrees" / "elsewhere" / "gitdir").write_text(f"{outside / '.git'}\n", encoding="utf-8")
        expected = f"git remote origin: {origin / 'worktrees'}, which sends Git to another folder"
    elif redirect == "linked-refs":
        heads = origin / "refs" / "heads"
        shutil.rmtree(heads)
        links.append(heads)
        expected = f"git remote origin: {heads}, which is not a plain file or folder"
    elif redirect == "settings-include":
        _git(origin, "config", "include.path", str(outside / "config"))
        expected = settings
    elif redirect == "settings-promisor":
        _git(origin, "config", "remote.backup.url", str(outside))
        _git(origin, "config", "remote.backup.promisor", "true")
        expected = settings
    elif redirect == "worktree-settings-include":
        _git(origin, "config", "extensions.worktreeConfig", "true")
        _git(origin, "config", "--worktree", "include.path", str(outside / "config"))
        expected = settings.replace(str(origin / "config"), str(origin / "config.worktree"))
    else:
        _git(origin, "config", "uploadpack.blobPackfileUri", f"{'0' * 40} {'0' * 40} https://example.invalid/pack")
        expected = settings
    for link in links:
        _dir_link(link, outside / "refs" / "heads")
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    try:
        assert cli.main(["--root", str(repo), "janitor", "--release-report"]) == 2
    finally:
        for link in links:
            _unlink_dir(link)

    assert _upload_packs(trace) == []
    assert report.read_text(encoding="utf-8").splitlines() == [expected + _UNPLACED]


def _windowless_gateway_run(fence: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """``python -m agenttalk --root <fence> gateway run`` in this process with no stdout and no
    stderr, as pythonw.exe starts it, reaching only a stub CLI that prints one line: no gateway
    code, task, ledger or network. Returns its exit code."""
    stub = ModuleType("agenttalk.cli")

    def console_main() -> int:
        print("synthetic startup diagnostic")
        return 0

    stub.console_main = console_main
    monkeypatch.setitem(sys.modules, "agenttalk.cli", stub)
    monkeypatch.setattr(sys, "argv", ["agenttalk", "--root", str(fence), "gateway", "run"])
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        with pytest.raises(SystemExit) as ended:
            runpy.run_module("agenttalk", run_name="__main__", alter_sys=True)
    finally:
        routed = sys.stderr
        if routed is not None:
            routed.close()
    return ended.value.code


@pytest.mark.parametrize("layout", ["outside-appdata", "folder-linked-out", "rotated-copy-linked-out"])
def test_the_early_gateway_log_is_refused_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str,
) -> None:
    """Round 16: a windowless gateway run sets up its log before the CLI loads, and made its
    folder, opened it and set its permissions outside the fence with no refusal. The log, the
    folders made for it and its rotated copies are now checked first: outside, the refusal is
    reported and the run stops with exit 2 before anything is made, opened or renamed."""
    fence, _, report = fenced
    fence = fence.resolve()
    outside = (tmp_path / "outside-appdata").resolve()
    appdata = outside if layout == "outside-appdata" else fence / "appdata"
    log = appdata / "agenttalk-ovh" / "gateway" / "gateway.log"
    if layout == "folder-linked-out":
        outside.mkdir()
        appdata.mkdir()
        _dir_link(appdata / "agenttalk-ovh", outside)
        link, refused = appdata / "agenttalk-ovh", f"{log} -> {outside / 'gateway' / 'gateway.log'}"
    elif layout == "rotated-copy-linked-out":
        outside.mkdir()
        log.parent.mkdir(parents=True)
        _dir_link(Path(f"{log}.1"), outside)
        link, refused = Path(f"{log}.1"), f"{log}.1 -> {outside}"
    else:
        link, refused = None, str(log)
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    try:
        rc = _windowless_gateway_run(fence, monkeypatch)
    finally:
        if link is not None:
            _unlink_dir(link)

    assert rc == 2
    assert not outside.exists() or list(outside.iterdir()) == []
    assert report.read_text(encoding="utf-8").splitlines() == [refused]


def test_the_early_gateway_log_inside_the_fence_still_works(
    fenced, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 16: a log inside the fence is written as before."""
    fence, _, report = fenced
    fence = fence.resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(fence / "appdata"))

    assert _windowless_gateway_run(fence, monkeypatch) == 0

    log = fence / "appdata" / "agenttalk-ovh" / "gateway" / "gateway.log"
    assert "synthetic startup diagnostic" in log.read_text(encoding="utf-8")
    assert not report.exists()


def test_the_early_gateway_log_without_a_fence_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 16: with no fence set, the log goes where it always did, unchecked."""
    monkeypatch.delenv("AGENTTALK_STORE_FENCE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    assert _windowless_gateway_run(tmp_path, monkeypatch) == 0

    log = tmp_path / "appdata" / "agenttalk-ovh" / "gateway" / "gateway.log"
    assert "synthetic startup diagnostic" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("linked", ["gateway.log", "gateway.log.1"])
def test_the_early_gateway_log_is_refused_when_it_has_a_name_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str,
) -> None:
    """Round 17: the log inside the fence was also a hard link to a file outside it, so the run
    appended to the outside file with no refusal. A log or rotated copy with a second name now
    passes only when all its names lie inside the fence, the rule a store's state folder
    follows; here the run stops with exit 2 and the outside file is untouched."""
    fence, _, report = fenced
    fence = fence.resolve()
    appdata = fence / "appdata"
    log = appdata / "agenttalk-ovh" / "gateway" / "gateway.log"
    log.parent.mkdir(parents=True)
    outside = (tmp_path / "outside-name.txt").resolve()
    outside.write_text("untouched\n", encoding="utf-8")
    os.link(outside, log.parent / linked)
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))

    assert _windowless_gateway_run(fence, monkeypatch) == 2

    assert outside.read_text(encoding="utf-8") == "untouched\n"
    assert report.read_text(encoding="utf-8").splitlines() == [f"{log.parent / linked} (has another name)"]


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_refuses_an_origin_file_with_a_name_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Round 17: a commit object of origin lay outside the fence, with a second name (a hard link)
    inside origin, and both modes fetched that commit. A file of origin with several names now
    passes only when all of them lie inside the fence. Objects a local clone shares with this
    repository still pass: both their names are inside."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    origin = fence / "remote.git"
    tree = _git(origin, "rev-parse", "HEAD^{tree}")
    only_outside = _git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit-tree", tree, "-p", "HEAD", "-m", "outside only")
    _git(origin, "update-ref", "refs/heads/main", only_outside)
    obj = origin / "objects" / only_outside[:2] / only_outside[2:]
    outside = (tmp_path / "outside-object").resolve()
    obj.replace(outside)
    os.link(outside, obj)
    trace = tmp_path / "git-trace.txt"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 2
    assert _upload_packs(trace) == []
    assert only_outside not in _git(repo, "for-each-ref", "--format=%(objectname)")
    assert (checkout / ".git").exists()
    assert report.read_text(encoding="utf-8").splitlines() == [f"{obj} (has another name)"]


def test_janitor_release_with_shared_objects_still_works_beside_an_unrelated_outside_link(
    fenced, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Round 17: the other names of origin's files are looked for across the whole fence, and a
    link elsewhere in the fence that leads outside is passed over, not refused. Here origin, a
    local clone, shares its object files with this repository, and the fence also holds a
    folder link to an outside folder."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    objects = [path for path in (fence / "remote.git" / "objects").rglob("*") if path.is_file()]
    assert any(os.lstat(path).st_nlink > 1 for path in objects)
    (tmp_path / "outside-folder").mkdir()
    _dir_link(fence / "zz-outside-link", tmp_path / "outside-folder")
    try:
        rc = cli.main(["--root", str(repo), "janitor", "--release-report"])
    finally:
        _unlink_dir(fence / "zz-outside-link")

    assert rc == 0, capsys.readouterr().out
    assert not report.exists()


@pytest.mark.parametrize("mode", ["--release", "--release-report"])
def test_janitor_release_keeps_the_spaces_in_origin_s_name(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: str,
) -> None:
    """Round 17, automated comment 4236579571: the check trimmed the spaces from the address Git
    gave for origin, so an origin named ' origin.git' was read as the different repository
    'origin.git', and both modes fetched that one. Only the line end Git adds is removed now,
    so release asks the repository origin really names."""
    fence, _, report = fenced
    fence = fence.resolve()
    checkout = fence / "scratch" / "done"
    repo = _merged_checkout(fence, checkout, fence / "scratch")
    _git(repo, "clone", "--bare", "--no-hardlinks", str(repo), str(repo / " origin.git"))
    trimmed = repo / "origin.git"
    _git(repo, "clone", "--bare", "--no-hardlinks", str(repo), str(trimmed))
    tree = _git(trimmed, "rev-parse", "HEAD^{tree}")
    only_trimmed = _git(trimmed, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit-tree", tree, "-p", "HEAD", "-m", "only in the trimmed name")
    _git(trimmed, "update-ref", "refs/heads/main", only_trimmed)
    _git(repo, "remote", "set-url", "origin", " origin.git")

    rc = cli.main(["--root", str(repo), "janitor", mode, *([str(checkout)] if mode == "--release" else [])])

    assert rc == 0, capsys.readouterr().out
    assert only_trimmed not in _git(repo, "for-each-ref", "--format=%(objectname)")
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == _git(repo, "rev-parse", "main")
    assert not report.exists()


@pytest.mark.skipif(os.name == "nt", reason="a file symlink needs a privilege on Windows")
def test_a_store_link_to_a_file_with_a_name_outside_the_fence_is_refused(fenced, tmp_path: Path) -> None:
    """Round 17, automated comment 4236579569: a symlink in the state folder led to a file inside
    the fence that also had a name outside it, and the walk took the link for a file with
    nothing to check. The file a link leads to must now have all its names in the state folder,
    like any file there."""
    fence, _, report = fenced
    fence = fence.resolve()
    project = fence / "project"
    Store(project).init(["lead"])
    target = fence / "elsewhere.txt"
    outside = (tmp_path / "outside-name.txt").resolve()
    outside.write_text("outside\n", encoding="utf-8")
    os.link(outside, target)
    pointer = project / ".agenttalk" / "pointer"
    os.symlink(target, pointer)

    assert cli.main(["--root", str(project), "roster"]) == 2

    assert report.read_text(encoding="utf-8").splitlines() == [f"{project} -> {pointer} (has another name)"]


def _below_a_file_on_every_system(monkeypatch: pytest.MonkeyPatch, below: Path) -> None:
    """Make looking up `below` fail as it does on Linux and macOS when a plain file stands where
    one of its folders would be (Windows reports such a path as missing instead)."""
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path) == str(below):
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


@pytest.mark.parametrize("how", ["real", "simulated"])
def test_the_second_name_check_takes_a_path_below_a_file_as_missing(
    fenced, monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    """Round 18, CI on Linux and macOS: a gateway log asked for below a plain file inside the
    fence was refused as a place that cannot be inspected, so the fallback for a log that
    cannot be opened never ran. Such a path cannot exist, and now counts as missing on every
    system, as it already did on Windows."""
    from agenttalk import store as store_mod

    fence, _, report = fenced
    blocker = fence.resolve() / "a file, not a folder"
    blocker.write_text("x", encoding="utf-8")
    below = blocker / "gateway.log"
    if how == "simulated":
        _below_a_file_on_every_system(monkeypatch, below)

    store_mod.check_second_names([below])

    assert not report.exists()


@pytest.mark.parametrize("how", ["real", "simulated"])
def test_the_origin_scan_takes_an_alternates_file_below_a_file_as_missing(
    fenced, monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    """Round 18: the origin scan looks up objects/info/alternates the same way, so a folder Git
    may take as origin whose objects entry is a plain file was refused on Linux and macOS and
    passed on Windows. Nothing can be borrowed through a path that cannot exist."""
    from agenttalk.worktree_release import _refuse_alternates

    fence, _, report = fenced
    objects = fence.resolve() / "objects"
    objects.write_text("x", encoding="utf-8")
    if how == "simulated":
        _below_a_file_on_every_system(monkeypatch, objects / "info" / "alternates")

    _refuse_alternates(objects, "git remote origin")

    assert not report.exists()


@pytest.mark.skipif(os.name == "nt", reason="a file symlink needs a privilege on Windows")
def test_a_store_link_that_leads_below_a_plain_file_is_taken_as_missing(fenced) -> None:
    """Round 18: the store walk looks up the file a link in the state folder leads to (round 17)
    and took a link leading below a plain file for one that cannot be inspected. It leads to
    no file, like a link to a missing path."""
    fence, _, report = fenced
    fence = fence.resolve()
    project = fence / "project"
    Store(project).init(["lead"])
    blocker = fence / "a file, not a folder"
    blocker.write_text("x", encoding="utf-8")
    os.symlink(blocker / "child", project / ".agenttalk" / "pointer")

    assert cli.main(["--root", str(project), "roster"]) == 0

    assert not report.exists()


@pytest.mark.parametrize("mode", [[], ["--apply"]], ids=["report", "apply"])
def test_janitor_checks_the_worktrees_folder_first(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: list[str],
) -> None:
    """Round 3, finding 4: a .worktrees junction to an outside folder was listed in report
    mode. The .worktrees folder is checked with the other places, before anything is read."""
    from agenttalk import janitor

    fence, outside, report = fenced
    inside = fence / "project"
    Store(inside).init(["lead"])
    _set_scratch(inside, root=str(fence / "scratch"), tmp_root=str(fence / "tmp"))
    linked = tmp_path / "outside-worktrees"
    (linked / "old-checkout").mkdir(parents=True)
    (linked / "old-checkout" / "note.txt").write_text("kept", encoding="utf-8")
    before = _snapshot(linked)
    listed: list[Path] = []
    real_iterdir = janitor._safe_iterdir
    monkeypatch.setattr(janitor, "_safe_iterdir", lambda path: listed.append(Path(path)) or real_iterdir(path))
    monkeypatch.setattr(janitor, "get_registered_worktrees", lambda repo: [inside])
    monkeypatch.setattr(janitor, "_registered_worktrees_listed", lambda repo: [inside])  # discovery's list
    monkeypatch.setattr(janitor, "is_dirty_worktree", lambda path: False)
    monkeypatch.setattr(janitor, "_run_git", lambda *args, **kwargs: (0, "", ""))   # no real git, on any code
    monkeypatch.setattr(janitor, "_run_git_checked", lambda *args, **kwargs: (0, "", ""))
    _dir_link(inside / ".worktrees", linked)
    try:
        assert cli.main(["--root", str(inside), "janitor", *mode]) == 2
        assert inside / ".worktrees" not in listed and inside.resolve() / ".worktrees" not in listed
        assert _snapshot(linked) == before
        assert report.read_text(encoding="utf-8").splitlines() == [
            f"{inside.resolve() / '.worktrees'} -> {linked.resolve()}"]
    finally:
        _unlink_dir(inside / ".worktrees")


@pytest.mark.parametrize("answers, allowed", [
    ([("lock", "linked"), None], True),  # a lock's second name, gone on the next look
    ([("lock", "linked")] * 10, False),  # a second name that stays
    ([("out", "outside")], False),  # never retried
])
def test_store_fence_allows_settles_a_passing_second_name_like_the_fence_check(
    tmp_path, monkeypatch, answers, allowed
):
    """store_fence_allows only reads, and it gives the same answer check_store_fence would: a lock being taken
    shows a file's second name for a moment, so a 'linked' answer is looked at again before it counts."""
    from agenttalk import store as store_mod

    monkeypatch.setenv(store_mod.STORE_FENCE_ENV, str(tmp_path))
    seen = list(answers)
    monkeypatch.setattr(store_mod, "_first_escape", lambda root, fence: seen.pop(0) if len(seen) > 1 else seen[0])
    monkeypatch.setattr(store_mod.time, "sleep", lambda seconds: None)
    assert store_mod.store_fence_allows(tmp_path / "team") is allowed


# --- #858 fix round 16: a path that an activity record names is fenced before anything resolves it -----------------

_NAMED_MARK = "zz-record-named"  # in the name of the folder that a record names outside the fence (lower case)
_NETWORK_MARK = "server-858"  # in the name of the network host of a UNC path


def _record_path(kind: str, tmp_path: Path) -> str:
    """An absolute path a record can name, outside the fence: a folder beside it, or a network path (UNC)."""
    if kind == "outside":
        folder = tmp_path / _NAMED_MARK
        folder.mkdir(exist_ok=True)
        return str(folder.resolve() / "x")
    return f"\\\\{_NETWORK_MARK}\\share\\x" if os.name == "nt" else f"//{_NETWORK_MARK}/share/x"


def _looked_at(monkeypatch: pytest.MonkeyPatch, *marks: str) -> list[tuple[str, str]]:
    """Every look at a path whose name holds one of `marks`, by the calls that ask the file system about a path (stat,
    lstat, realpath, exists, a listing, a link's target). The spy answers an error and never asks the real call, so a
    network path is never contacted. The fence's own resolver is not exempted: a look it makes is recorded too."""
    looked: list[tuple[str, str]] = []

    def watch(name, real):
        def spy(path, *args, **kwargs):
            try:
                text = os.fsdecode(os.fspath(path))
            except TypeError:  # a file descriptor
                return real(path, *args, **kwargs)
            if any(mark in os.path.normcase(text) for mark in marks):
                looked.append((name, text))
                raise OSError(errno.EIO, "a path outside the fence was looked at (spy)")
            return real(path, *args, **kwargs)

        return spy

    calls = [(os, "lstat"), (os, "stat"), (os, "scandir"), (os, "listdir"), (os, "readlink"), (os.path, "realpath"),
             (os.path, "exists"), (os.path, "lexists"), (os.path, "isdir"), (os.path, "isfile")]
    if os.name == "nt":
        import nt

        calls.append((nt, "_getfinalpathname"))
    for owner, name in calls:
        monkeypatch.setattr(owner, name, watch(f"{owner.__name__}.{name}", getattr(owner, name)))
    return looked


_RECORDS = {
    "lane-worktree_path": ("lane", "worktree_path"),
    "lane-cwd": ("lane", "cwd"),
    "launch-cwd": ("launch", "cwd"),
}


def _write_named(repo: Path, record: str, text: str) -> None:
    """A record that names `text`: an active lane row, or a pending launch request."""
    kind, field = _RECORDS[record]
    state = repo / ".agenttalk" / "state"
    state.mkdir(parents=True, exist_ok=True)
    if kind == "lane":
        row = {"status": "active", field: text}
        (state / "lanes.json").write_text(json.dumps({"lanes": {"one": row}}), encoding="utf-8")
    else:
        (state / "launch-requests").mkdir(exist_ok=True)
        row = {"state": "pending", field: text}
        (state / "launch-requests" / "r.json").write_text(json.dumps(row), encoding="utf-8")


def _swept_checkout(fence: Path) -> tuple[Path, Path]:
    """(repo, the merged, clean checkout `.worktrees/done`): a candidate of the sweep, and one a release removes."""
    done = fence / "repo" / ".worktrees" / "done"
    return _merged_checkout(fence, done, fence / "scratch"), done


@pytest.mark.parametrize("mode", _JANITOR_MODES)
@pytest.mark.parametrize("named", ["outside", "network"])
@pytest.mark.parametrize("record", list(_RECORDS))
def test_a_record_that_names_a_path_outside_the_fence_keeps_the_checkout_and_nothing_outside_is_looked_at(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    record: str, named: str, mode: list[str],
) -> None:
    """The in-use check reads every record, and each path a record names went through `lstat` and `realpath` before it
    was judged unrelated: the preflight fences the configured roots and the registered checkouts, not these. Now the
    fence decides first, from the path as written (no look at it, so a network path is never contacted), and a path it
    refuses keeps the checkout and says that the record names a path outside the fence: never read as unrelated."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    _write_named(repo, record, _record_path(named, tmp_path))
    looked = _looked_at(monkeypatch, _NAMED_MARK, _NETWORK_MARK)

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert looked == [], f"a path outside the fence was looked at: {looked}"
    assert (done / ".git").exists() and str(done) in _git(repo, "worktree", "list").replace("/", os.sep), text
    assert "names a path outside the fence" in text and "kept" in text.lower(), text
    assert rc in (0, 1), text  # a kept checkout is a refusal, not a crash


@pytest.mark.parametrize("mode", _JANITOR_MODES)
def test_a_record_that_names_a_path_inside_the_fence_that_is_not_the_checkout_frees_it_as_before(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: list[str],
) -> None:
    """The control: an active lane and a launch request that name some other folder inside the fence are placed as
    ever (and looked at: the fence allows it); the checkout is released or swept, with no refusal."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    elsewhere = fence / "elsewhere"
    elsewhere.mkdir()
    _write_named(repo, "lane-cwd", str(elsewhere))
    _write_named(repo, "launch-cwd", str(elsewhere))

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert "outside the fence" not in text and f"REFUSED (kept) {done}" not in text and f"KEPT {done}" not in text, text
    assert not report.exists()
    if "--release-report" in mode:
        assert f"WOULD RELEASE {done}" in text, text
    elif "--apply" in mode or "--release" in mode:
        assert not done.exists() and rc == 0, text


@pytest.mark.parametrize("mode", _JANITOR_MODES)
@pytest.mark.parametrize("record", ["lane-worktree_path", "launch-cwd"])
def test_a_record_that_names_the_checkout_inside_the_fence_still_keeps_it(
    fenced, tmp_path: Path, capsys: pytest.CaptureFixture, record: str, mode: list[str],
) -> None:
    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    _write_named(repo, record, str(done))

    cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert (done / ".git").exists(), text
    assert ("names this checkout" in text or "REFUSED (kept)" in text or "KEPT" in text), text
    assert "outside the fence" not in text, text


@pytest.mark.parametrize("mode", _JANITOR_MODES)
def test_a_record_that_names_an_in_fence_link_to_an_outside_folder_keeps_the_checkout(
    fenced, tmp_path: Path, capsys: pytest.CaptureFixture, mode: list[str],
) -> None:
    """A path that is inside the fence as written but leads outside through a link: the fence's own resolution follows
    the link before it refuses (#913), and then the checkout is kept, with the same reason."""
    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    outside = tmp_path / "elsewhere-outside"
    outside.mkdir()
    link = fence / "link-to-outside"
    _dir_link(link, outside)
    _write_named(repo, "lane-cwd", str(link / "x"))
    try:
        cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])
    finally:
        _unlink_dir(link)

    text = capsys.readouterr().out
    assert (done / ".git").exists() and "names a path outside the fence" in text, text


# --- #858 fix round 17: no look at a record-named path outside the fence on ANY in-use route, advice code included ----

_GUARD_FOLDER = "zz-guard-outside"  # in the name of the folder outside the fence that records name (lower case)
_GUARD_HOST = "server-858g"  # in the name of the network host of a UNC value


class _OutsideGuard:
    """What the guard saw. Every path is classified by the FENCE itself, never by where a test put things: a path is
    inside when, made absolute (a string operation), it is the fence or below it, as the fence is written or as it
    resolves; every other path is OUTSIDE, whatever it is and whichever host a network path names. An outside path is
    never forwarded to the real call: the guard records it in `outside` and answers an error, so nothing there (or
    on the network) is read.

    A path that is inside as written is judged by where the operating system would really go with it: the folders on
    its way are looked at (they are inside the fence) and, when one of them is a link whose target lies outside the
    fence, the look is an outside look, whichever call makes it (`lstat` of `link/x` follows `link`).

    `outside` also holds a resolution of an inside-as-written path that LED outside (`os.path.realpath` answered a path
    outside the fence: that is a link followed to its target, whether or not the platform's own resolver makes visible
    calls), and a second resolution inside one fence check.

    The one allowance is the fence's own resolution (#913): when `check_folder_fence` is given a path that is inside as
    written, ONE `os.path.realpath` of exactly that path is let through, and the calls that resolver makes inside it are
    not counted. It is recorded in `resolved_by_the_fence`, once per fence check, and a test asserts that list."""

    def __init__(self) -> None:
        self.outside: list[tuple[str, str]] = []
        self.resolved_by_the_fence: list[tuple[str, str]] = []
        self.fence_checks: list[str] = []
        self.looks: list[tuple[str, str]] = []  # every look at a path inside the fence, as written: (call, path)


def _install_guard(monkeypatch: pytest.MonkeyPatch, fence: Path) -> _OutsideGuard:
    """Replace every call that asks the file system about a path, and the Git runners, with a spy that classifies the
    path by the fence (see `_OutsideGuard`). Git itself is found once, before the spies, so that the search of the
    program folders is not read as a look outside."""
    import pathlib

    from agenttalk import janitor, worktree_release

    guard = _OutsideGuard()
    # The standard library looks for message catalogues of the system's language (`/usr/share/locale/...`) whenever
    # an error text or help is built, on systems that have one: that is the interpreter's own business, not the code
    # under test. The plain "C" language has no catalogue to look for, so the guard needs no allowance for them.
    for name in ("LANG", "LANGUAGE", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LC_ALL", "C")
    inside_roots = {os.path.normcase(os.path.abspath(fence)), os.path.normcase(os.path.realpath(fence))}
    # The folders ABOVE the fence are looked at (never listed) by the check of the configured roots, which walks every
    # parent from the drive root down ("a linked parent is refused"): a look at one of them is a look at the way to the
    # fence, not at a place a record names. Only those exact paths, and only by the calls that ask about one path.
    roots = (fence, Path(os.path.realpath(fence)))
    ancestors = {os.path.normcase(str(parent)) for root in roots for parent in root.parents}
    git_exe = shutil.which("git")
    real_realpath, real_which = os.path.realpath, shutil.which
    # "roots": the janitor's own identity checks of the configured roots and their parents are running; "resolving": a
    # resolver is working on a path inside the fence (on Linux it looks at each parent on its way, the fence's own).
    state = {"approved": 0, "scope": None, "scope_used": False, "roots": 0, "resolving": 0}
    real_lstat, real_readlink = os.lstat, os.readlink
    root_paths = [Path(root) for root in inside_roots]
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def is_link_status(status) -> bool:
        return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & reparse)

    def where_it_goes(text: str, *, follow_last: bool, depth: int = 0) -> tuple[str, str] | None:
        """(the link, where it leads) when the way from the fence to `text` meets a link whose target is outside the
        fence, else None. Only the folders on the way are looked at (all inside the fence, with the real calls); a link
        is read, never followed by the guard. A link that leads inside is followed on paper (the rest of the way is
        judged from its target). The last part is not followed unless `follow_last` (a call like `lstat`)."""
        path = Path(os.path.abspath(text))
        root = next((r for r in root_paths if os.path.normcase(str(path)).startswith(os.path.normcase(str(r)))), None)
        if root is None or depth > 8:
            return None
        parts = path.parts[len(root.parts):]
        current = root
        for index, part in enumerate(parts):
            current = current / part
            if index == len(parts) - 1 and not follow_last:
                break
            try:
                status = real_lstat(current)
            except OSError:
                return None  # nothing there: nothing is followed beyond it
            if is_link_status(status):
                target = real_readlink(current)
                if target.startswith("\\\\?\\"):  # the extended form Windows gives for a junction's target
                    target = target[4:]
                goes = Path(os.path.abspath(os.path.join(os.path.dirname(current), target)))
                if not any(
                    os.path.normcase(str(goes)) == os.path.normcase(str(r))
                    or os.path.normcase(str(goes)).startswith(os.path.normcase(str(r)) + os.sep)
                    for r in root_paths
                ):
                    return str(current), str(goes)
                rest = Path(*parts[index + 1:]) if parts[index + 1:] else Path()
                return where_it_goes(str(goes / rest), follow_last=follow_last, depth=depth + 1)
        return None

    def inside(text: str) -> bool:
        norm = os.path.normcase(os.path.abspath(text))
        return any(norm == root or norm.startswith(root + os.sep) for root in inside_roots)

    def text_of(value) -> str | None:
        try:
            return os.fsdecode(os.fspath(value))
        except TypeError:  # a file descriptor, or no path at all
            return None

    def refuse(name: str, text: str) -> None:
        guard.outside.append((name, text))
        raise OSError(errno.EIO, "a path outside the fence was looked at (guard)")

    # Git's own options, with Git's meaning: the guard knows these and nothing else. "switch": takes no value; "value":
    # may carry a value after "=" (judged like an argument; a value given as the next argument is judged as that
    # argument); "path": a path, after "=" or as the next argument; "folder": `-C`, a change of folder; "config": `-c`,
    # a `key=value` setting as the next argument; "git-path": `rev-parse --git-path P`, a path that Git reads against
    # its own Git folder, as the next argument (so only a plain word or an absolute path inside passes). The options
    # before the command (`git <options> <command>`) are not the command's: `-C` after the command is not a change of
    # folder (`git commit -C <commit>`). Any option not in these tables is recorded, with or without a value.
    before_command = {"-C": "folder", "-c": "config", "--git-dir": "path", "--work-tree": "path",
                      "--no-optional-locks": "switch", "--no-pager": "switch", "--no-replace-objects": "switch"}
    after_command = {
        **dict.fromkeys(("-z", "-v", "-q", "--quiet", "-f", "--force", "--others", "--ignored", "--exclude-standard",
                         "--symref", "--no-tags", "--no-prune", "--no-recurse-submodules", "--verify", "--get",
                         "--get-regexp", "--get-url", "--name-only", "--stdin", "--stdin-paths", "--no-filters",
                         "--stage", "--show-toplevel", "--is-ancestor", "--detach"), "switch"),
        **dict.fromkeys(("--porcelain", "--untracked-files", "--ignore-submodules", "--type", "--abbrev-ref",
                         "--contains", "--format"), "value"),
        "--separate-git-dir": "path",
        "--git-path": "git-path",
    }
    separators = os.sep + (os.altsep or "")

    def unplaceable(text: str, base: str | None) -> bool:
        """True unless `text` is provably a plain word (a name with no folder in it: a command, a branch, a remote, a
        ref) or a path inside the fence. Read as text only, nothing is looked at: a relative path is read against the
        folder Git is in (`base`), lexically, so `../../elsewhere` from `fence/repo` is beside the fence."""
        if not text:
            return False
        if "://" in text or "::" in text or text.startswith("~"):
            return True  # an address, a remote helper, a home folder Git expands: no place the guard can judge
        if os.path.isabs(text):
            return not inside(text)
        if os.path.splitdrive(text)[0]:
            return True  # a drive with no folder after it (`C:x`): read against that drive's own current folder
        first = re.split(f"[{re.escape(separators)}]", text, maxsplit=1)[0]
        if ":" in first:
            return True  # `host:path`, Git's short form for an address, or pathspec magic (`:/`)
        if not any(s in text for s in separators) and text != "..":
            return False  # a plain word
        return base is None or not inside(os.path.join(base, text))  # lexical: normalised by `inside`

    def unplaceable_setting(setting: str) -> bool:
        """A `-c key=value` setting: the key must be a plain setting name, and the value a plain word or an absolute
        path inside the fence. A relative path is not placeable: Git reads some settings (`core.worktree`) against
        its own folder, not against the folder it runs in."""
        key, _, value = setting.partition("=")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", key):
            return True
        return unplaceable(value, None)  # no folder to read a relative value against

    def outside_argument(repo_text: str | None, args: tuple) -> str | None:
        """The first argument of a Git runner that the guard cannot prove harmless, as written, or None. FAIL CLOSED:
        an argument passes only when it is a known option (see the tables above), a plain word, or a path inside the
        fence; an unknown option (with or without a value), a value given to a switch, an argument that is not text,
        and anything the guard cannot place are recorded. Git's order is followed: each `-C` before the command (as
        `-C P` or `-CP`) moves the folder that every later relative path is read against. After `--` every argument
        is a path. Read as text only: no path is looked at; a path object is read with `os.fspath`."""
        base = repo_text
        command = only_paths = False
        rest = list(args)
        while rest:
            argument = rest.pop(0)
            try:
                text = os.fsdecode(os.fspath(argument))
            except TypeError:
                return repr(argument)  # not text and not a path: the guard cannot read it
            if only_paths or not text.startswith("-") or text == "-":
                if unplaceable(text, base):
                    return text
                command = True  # the first plain word is the command
                continue
            if text == "--":
                only_paths = True
                continue
            if not command and text.startswith("-C") and text != "-C":
                name, value = "-C", text[2:]  # `-CP`, one argument
            else:
                name, equals, value = text.partition("=")
                value = value if equals else None
            kind = (after_command if command else before_command).get(name)
            if kind is None or (kind == "switch" and value is not None):
                return text if value is None else value  # unknown, or a switch given a value: the guard cannot tell
            if kind == "switch":
                continue
            if kind == "value":
                if value is not None and unplaceable(value, base):
                    return value
                continue
            if value is None:  # "path", "folder", "config" and "git-path" take the next argument
                if not rest:
                    return text  # the value is missing
                try:
                    value = os.fsdecode(os.fspath(rest.pop(0)))
                except TypeError:
                    return text
            elif kind in ("config", "git-path"):
                return text  # `-c=...` and `--git-path=...` are not Git's forms
            if kind == "config":
                if unplaceable_setting(value):
                    return value
                continue
            if kind == "git-path":
                if unplaceable(value, None):  # read against the Git folder, which the guard does not know
                    return value
                continue
            if unplaceable(value, base):
                return value
            if kind == "folder" and value:  # Git leaves the folder as it is for an empty `-C ""`
                if base is None and not os.path.isabs(value):
                    return value
                base = os.path.abspath(os.path.join(base or "", value))
        return None

    def on_the_way(text: str) -> bool:
        """An exact ancestor of the fence, looked at by one of the two things that expect to: the identity checks of the
        configured roots (`_ancestor_ids`, `_ancestor_change_reason`) and a resolution of a path inside the fence. A
        look at an ancestor from anything else, a reader that was handed one by a record, is an outside look."""
        if not (state["roots"] or state["resolving"]):
            return False
        return os.path.normcase(os.path.abspath(text)) in ancestors

    def judge(name: str, text: str, *, way_ok: bool, follow_last: bool) -> None:
        if state["approved"]:
            return
        if not inside(text):
            if not (way_ok and on_the_way(text)):
                refuse(name, text)
            return
        guard.looks.append((name, text))
        led = where_it_goes(text, follow_last=follow_last)
        if led is not None:  # inside as written, but the system follows a link on the way to a place outside
            guard.outside.append((f"{name} follows a link outside", f"{text} -> {led[1]} (through {led[0]})"))
            raise OSError(errno.EIO, "a link inside the fence leads outside (guard)")

    def watch(name: str, real, *, way_ok: bool = True, follow_last: bool = True):
        def spy(path, *args, **kwargs):
            text = text_of(path)
            if text is not None:
                judge(name, text, way_ok=way_ok, follow_last=follow_last)
            return real(path, *args, **kwargs)

        return spy

    def realpath_spy(path, *args, **kwargs):
        text = text_of(path)
        if text is None or state["approved"]:
            return real_realpath(path, *args, **kwargs)
        if not inside(text) and not on_the_way(text):
            refuse("os.path.realpath", text)
        if state["scope"] is not None and os.path.normcase(os.path.abspath(text)) == state["scope"]:
            if state["scope_used"]:
                refuse("a second resolution inside one fence check", text)
            state["scope_used"] = True
            state["approved"] += 1
            try:
                result = real_realpath(path, *args, **kwargs)
            finally:
                state["approved"] -= 1
            if not inside(result):
                guard.resolved_by_the_fence.append(("os.path.realpath", text))
            return result
        state["resolving"] += inside(text)
        try:
            result = real_realpath(path, *args, **kwargs)
        finally:
            state["resolving"] -= inside(text)
        if not inside(result):  # a link inside the fence was followed to a place outside it
            guard.outside.append(("os.path.realpath led outside", f"{text} -> {result}"))
        return result

    calls = [(os, "lstat"), (os, "stat"), (os, "scandir"), (os, "listdir"), (os, "readlink"), (os.path, "exists"),
             (os.path, "lexists"), (os.path, "isdir"), (os.path, "isfile"), (os.path, "samefile")]
    if os.name == "nt":
        import nt

        calls.append((nt, "_getfinalpathname"))
    for owner, name in calls:
        listing = name in ("scandir", "listdir", "readlink")  # these would show what is NEXT to the way, or beyond it
        no_follow = name in ("lstat", "readlink")  # the last part of the path is not followed by these
        monkeypatch.setattr(
            owner, name,
            watch(f"{owner.__name__}.{name}", getattr(owner, name), way_ok=not listing, follow_last=not no_follow))
    monkeypatch.setattr(os.path, "realpath", realpath_spy)
    for method in ("exists", "is_dir", "stat", "lstat", "iterdir", "resolve"):
        real_method = getattr(pathlib.Path, method)

        def path_spy(self, *args, _name=f"Path.{method}", _real=real_method, **kwargs):
            text = text_of(self)
            if text is not None:
                judge(_name, text, way_ok=_name != "Path.iterdir", follow_last=_name != "Path.lstat")
            if _name == "Path.resolve" and text is not None and inside(text):
                state["resolving"] += 1  # a resolver on a path inside the fence looks at the fence's parents on Linux
                try:
                    return _real(self, *args, **kwargs)
                finally:
                    state["resolving"] -= 1
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, method, path_spy)
    for owner, name in (
        (janitor, "_run_git_checked"), (janitor, "_run_git"),
        (worktree_release, "_read_git"), (worktree_release, "_git"),
    ):
        real_git = getattr(owner, name)

        def git_spy(repo, *args, _name=f"{owner.__name__}.{name}", _real=real_git, **kwargs):
            text = text_of(repo)
            if text is not None and not inside(text):
                refuse(_name, text)
            where = outside_argument(text, args)  # a path outside the fence handed to Git is a look at it too
            if where is not None:
                refuse(f"{_name} argument", where)
            return _real(repo, *args, **kwargs)

        monkeypatch.setattr(owner, name, git_spy)
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: git_exe if cmd == "git" else real_which(cmd, *a, **k))
    for name in ("_ancestor_ids", "_ancestor_change_reason"):  # the checks of the configured roots: they expect to look
        real_roots_check = getattr(janitor, name)

        def roots_spy(*args, _real=real_roots_check, **kwargs):
            state["roots"] += 1
            try:
                return _real(*args, **kwargs)
            finally:
                state["roots"] -= 1

        monkeypatch.setattr(janitor, name, roots_spy)

    real_check = cli.store_mod.check_folder_fence

    def check(folder):
        text = text_of(folder)
        guard.fence_checks.append(str(text))
        state["scope"] = os.path.normcase(os.path.abspath(text)) if text is not None and inside(text) else None
        state["scope_used"] = False
        try:
            return real_check(folder)
        finally:
            state["scope"] = None

    monkeypatch.setattr(cli.store_mod, "check_folder_fence", check)
    return guard


def _guard_value(kind: str, tmp_path: Path, fence: Path) -> str:
    """An absolute path a record can name: a folder beside the fence, a network path (UNC), or a link inside the fence
    that leads to a folder outside it."""
    if kind == "outside":
        folder = tmp_path / _GUARD_FOLDER
        folder.mkdir(exist_ok=True)
        return str(folder.resolve() / "x")
    if kind == "network":
        return f"\\\\{_GUARD_HOST}\\share\\x" if os.name == "nt" else f"//{_GUARD_HOST}/share/x"
    target = tmp_path / "zz-guard-link-target"
    target.mkdir(exist_ok=True)
    _dir_link(fence / "link-to-outside", target)
    return str(fence / "link-to-outside" / "x")


# Every shape of record that the in-use check reads, with every field that names a folder filled with the value `v`, and
# the neighbours of a damaged row blank or damaged: (where it is written, what is written).
_GUARD_SHAPES = {
    "lane-every-field": ("lane", lambda v: {
        "status": "active", "worktree_path": v, "worktree_toplevel_canonical": v, "cwd": v, "launch_cwd": v,
        "workspace_path": v}),
    "lane-worktree-path-blank": ("lane", lambda v: {
        "status": "active", "worktree_path": "", "worktree_toplevel_canonical": v}),
    "lane-canonical-blank": ("lane", lambda v: {
        "status": "active", "worktree_path": v, "worktree_toplevel_canonical": ""}),
    "lane-worktree-path-relative": ("lane", lambda v: {
        "status": "active", "worktree_path": "relative/folder", "worktree_toplevel_canonical": v}),
    "lane-pending-delivery-copy": ("lane", lambda v: {
        "status": "delivered", "publish_pending": {"lane": {"worktree_path": "", "worktree_toplevel_canonical": v}},
        "occupancy_worktrees": [v]}),
    "lane-retained-occupancy": ("lane", lambda v: {"status": "abandoned", "occupancy_worktrees": [v]}),
    # A saved copy that sits inside a row that is NOT a pending delivery (a hand-edited or damaged record): its advice
    # is not cut short by "recover the delivery first", so it is the case that reaches the look at the other path.
    "lane-saved-copy-in-an-active-row": ("lane", lambda v: {
        "status": "active", "saved": {"worktree_path": "", "worktree_toplevel_canonical": v}}),
    "launch-request": ("launch", lambda v: {
        "state": "pending", "cwd": v, "launch_cwd": v, "workspace_path": v, "worktree_path": v}),
    "supervisor-state": ("supervisor-state", lambda v: {"agents": {"seat": {"cwd": v, "launch_cwd": v}}}),
    "store-supervisor": ("store-supervisor", lambda v: {"agents": {"seat": {"launch_cwd": v}}}),
}


def _write_guard_record(repo: Path, shape: str, value: str) -> None:
    where, build = _GUARD_SHAPES[shape]
    store = repo / ".agenttalk"
    state = store / "state"
    state.mkdir(parents=True, exist_ok=True)
    row = build(value)
    if where == "lane":
        (state / "lanes.json").write_text(json.dumps({"lanes": {"one": row}}), encoding="utf-8")
    elif where == "launch":
        (state / "launch-requests").mkdir(exist_ok=True)
        (state / "launch-requests" / "r.json").write_text(json.dumps(row), encoding="utf-8")
    elif where == "supervisor-state":
        (state / "supervisor-state.json").write_text(json.dumps(row), encoding="utf-8")
    else:
        (store / "supervisor.json").write_text(json.dumps(row), encoding="utf-8")


@pytest.mark.parametrize("mode", _JANITOR_MODES)
@pytest.mark.parametrize("kind", ["outside", "network", "link-out"])
@pytest.mark.parametrize("shape", list(_GUARD_SHAPES))
def test_no_in_use_route_looks_at_a_record_named_path_outside_the_fence(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    shape: str, kind: str, mode: list[str],
) -> None:
    """The structural guard (rounds 14 to 17 each found one more unfenced read, one reader at a time). Every in-use
    route (the report, `--apply`, `--release-report`, `--release`) is driven with each shape of record, every field that
    names a folder filled with an outside path, a network path or an in-fence link that leads out, and damaged or blank
    neighbours; the calls that ask the file system about a path, and the two Git runners, are spies that record any look
    at a path outside the fence (every path is classified by the fence itself and an outside path is never forwarded;
    a link followed to a target outside is a look too). Zero looks outside; the checkout is kept. For the link, the
    fence's own resolution (#913) is the one allowed look: exactly one per fence check, counted and asserted."""
    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    value = _guard_value(kind, tmp_path, fence)
    _write_guard_record(repo, shape, value)
    guard = _install_guard(monkeypatch, fence)

    cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert guard.outside == [], f"a path outside the fence was looked at: {guard.outside}"
    assert (done / ".git").exists() and str(done) in _git(repo, "worktree", "list").replace("/", os.sep), text
    if kind == "link-out" and mode == ["--apply"]:
        # A record path that leads outside the fence is a refusal by the fence itself: the checkout is kept, as a
        # failure that stops the sweep (no prune), not as an ordinary "in use" keep.
        assert "worktree prune skipped" in text and "store fence refused" in text, text
    else:
        assert "REFUSED (kept)" in text or "KEPT" in text, text  # the checkout is kept, with a reason
    named = os.path.normcase(os.path.abspath(value))
    given = [c for c in guard.fence_checks if os.path.normcase(os.path.abspath(c)) == named]
    if kind == "link-out":
        # The accepted resolution (#913), counted: exactly one `realpath` of the link path per fence check that was
        # handed it, and nothing else. An extra look outside, or a second resolution, is in `outside` and fails above.
        assert given, "the fence was never asked about the link"
        expected = [("os.path.realpath", c) for c in given]
        assert guard.resolved_by_the_fence == expected, (given, guard.resolved_by_the_fence)
    else:
        assert guard.resolved_by_the_fence == [], guard.resolved_by_the_fence  # the fence resolved nothing outside


@pytest.mark.parametrize("mode", _JANITOR_MODES)
@pytest.mark.parametrize(
    "where, advice",
    [("elsewhere", "names a folder that still exists"), ("missing-folder", "to retire this lane without using")],
    ids=["a-folder-that-is-there", "a-folder-that-is-gone"],
)
def test_a_damaged_record_that_names_an_in_fence_path_still_gets_its_recovery_advice(
    fenced, tmp_path: Path, capsys: pytest.CaptureFixture, mode: list[str], where: str, advice: str,
) -> None:
    """The control: the same damaged row (`worktree_path` blank, an absolute `worktree_toplevel_canonical`) with a path
    INSIDE the fence is still looked at and still gets the advice that says what is there (the other saved path names a
    folder that exists, or nothing is there), in every mode, with the checkout kept."""
    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    named = fence / where
    if where == "elsewhere":
        named.mkdir()
    row = {"status": "active", "worktree_path": "", "worktree_toplevel_canonical": str(named)}
    state = repo / ".agenttalk" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "lanes.json").write_text(json.dumps({"lanes": {"one": row}}), encoding="utf-8")

    cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = " ".join(capsys.readouterr().out.split())
    assert (done / ".git").exists(), text
    assert advice in text and "outside the fence" not in text, text


@pytest.mark.parametrize("network", [False, True], ids=["local", "network"])
def test_the_recovery_advice_of_a_damaged_lane_row_does_not_look_at_its_other_outside_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network: bool,
) -> None:
    """The review's probe, kept: a row with `worktree_path` empty and an absolute `worktree_toplevel_canonical` outside
    the fence reached the advice (which looks at that other path to say whether something is still there) before the
    path was fenced."""
    from agenttalk import worktree_release

    fence = tmp_path / "fence"
    repo = fence / "repo"
    repo.mkdir(parents=True)
    value = (f"\\\\{_GUARD_HOST}\\share\\x" if os.name == "nt" else f"//{_GUARD_HOST}/share/x") if network else str(
        tmp_path / _GUARD_FOLDER / "x")
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    guard = _install_guard(monkeypatch, fence)
    row = {"status": "active", "worktree_path": "", "worktree_toplevel_canonical": value}

    with pytest.raises(worktree_release.Refused, match="outside the fence"):
        worktree_release._names_checkout(row, repo / "checkout", repo, lane_id="one", lane=True)

    assert guard.outside == [], guard.outside


@pytest.mark.parametrize(
    "mode", [pytest.param(["--apply"], id="apply"), pytest.param(["--release", "{done}"], id="release")]
)
@pytest.mark.parametrize("kind", ["outside", "network"])
@pytest.mark.parametrize("shape", ["lane-worktree-path-blank", "launch-request"])
def test_a_record_written_after_the_first_look_is_fenced_at_the_last_look_too(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    shape: str, kind: str, mode: list[str],
) -> None:
    """The first look finds nothing; the record that names an outside path appears just before the last look (the
    re-read under the lane writers' lock, made by the sweep and by a release before the removal). That look goes through
    the same reader and the same fence: nothing outside is looked at and the checkout is kept, not removed."""
    from agenttalk import worktree_release

    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    value = _guard_value(kind, tmp_path, fence)
    guard = _install_guard(monkeypatch, fence)
    real_reread = worktree_release.RemovalGuard.reread

    def late(self):
        _write_guard_record(repo, shape, value)
        return real_reread(self)

    monkeypatch.setattr(worktree_release.RemovalGuard, "reread", late)

    cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert guard.outside == [], f"a path outside the fence was looked at: {guard.outside}"
    assert (done / ".git").exists() and (done / "source.txt").exists(), text  # kept, not removed
    assert "REFUSED (kept)" in text or "KEPT" in text, text


def test_the_guard_never_forwards_a_path_outside_the_fence_whatever_it_is(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard answers for ANY path outside the fence, not only for the ones a fixture made: a folder beside the
    fence, a place on the drive root (beyond the folder of the test), a network path on one host and on ANOTHER host, a
    relative path (the working folder is outside the fence). Each is recorded and answered with an error (`EIO`, which
    the real call would never give for these), so the path is never touched and no network host is contacted."""
    fence, _, _ = fenced
    fence = fence.resolve()
    guard = _install_guard(monkeypatch, fence)
    anchor = Path(tmp_path.anchor)
    network = ("\\\\other-host-858\\share\\x", "\\\\" + _GUARD_HOST + "\\share\\x") if os.name == "nt" else (
        "//other-host-858/share/x", "//" + _GUARD_HOST + "/share/x")
    outside = [str(tmp_path / "beside-the-fence"), str(anchor / "zz-beyond-the-test-folder"), *network, "relative-name"]
    looks = [os.lstat, os.stat, os.path.exists, os.path.realpath, os.scandir, lambda place: Path(place).exists()]
    for place in outside:
        for look in looks:
            try:
                look(place)
            except OSError as exc:
                assert exc.errno == errno.EIO, (place, exc)  # the guard's own answer, not the file system's
            else:
                raise AssertionError(f"a look was let through for {place}")
    assert len(guard.outside) == len(outside) * 6, guard.outside
    inside_place = fence / "inside-the-fence"
    inside_place.mkdir()
    assert os.path.exists(inside_place) and os.path.realpath(inside_place) == str(inside_place)  # inside: forwarded
    assert len(guard.outside) == len(outside) * 6 and guard.resolved_by_the_fence == []


def test_the_guard_bounds_what_the_fence_may_resolve(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one allowance is the fence's own resolution of the ONE path it is given. A fence check that also makes an
    extra read of an outside path, or resolves a second time, is not let through: both are recorded as outside looks."""
    from agenttalk import store as store_mod

    fence, _, _ = fenced
    fence = fence.resolve()
    target = tmp_path / "zz-guard-self-test-target"
    target.mkdir()
    link = fence / "self-test-link"
    _dir_link(link, target)
    extra = tmp_path / "zz-guard-self-test-extra"

    def greedy_check(folder):
        os.path.realpath(folder)  # the accepted resolution
        for extra_look in (lambda: os.path.realpath(folder), lambda: os.lstat(extra)):
            try:
                extra_look()  # a second resolution in the same check; an extra read of another outside path
            except OSError:
                pass

    monkeypatch.setattr(store_mod, "check_folder_fence", greedy_check)
    guard = _install_guard(monkeypatch, fence)

    store_mod.check_folder_fence(link / "x")

    assert guard.resolved_by_the_fence == [("os.path.realpath", str(link / "x"))], guard.resolved_by_the_fence
    names = [name for name, _ in guard.outside]
    assert "a second resolution inside one fence check" in names, guard.outside
    assert ("os.lstat", str(extra)) in guard.outside, guard.outside


def test_the_guard_counts_a_link_followed_outside_by_anything_but_the_fence_check(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution of an inside-as-written link that ends outside the fence is a look at the target, whether or not the
    platform's resolver makes calls the guard can see (on Windows it does not): outside a fence check it is recorded."""
    fence, _, _ = fenced
    fence = fence.resolve()
    target = tmp_path / "zz-guard-self-test-target2"
    target.mkdir()
    link = fence / "self-test-link2"
    _dir_link(link, target)
    guard = _install_guard(monkeypatch, fence)

    os.path.realpath(link)  # not inside a fence check, as the preflight's walk did

    # Where the resolver is written in Python (Linux, macOS) the outside target is looked at by visible calls as well,
    # and those are recorded too; where it is not (Windows) the result of the resolution is the only evidence.
    assert "os.path.realpath led outside" in [name for name, _ in guard.outside], guard.outside
    assert guard.resolved_by_the_fence == []


@pytest.mark.parametrize(
    "mode", [pytest.param(["--apply"], id="apply"), pytest.param(["--release", "{done}"], id="release")]
)
@pytest.mark.parametrize("shape", ["lane-every-field", "launch-request"])
def test_a_valid_in_fence_record_of_a_missing_folder_reaches_the_final_recheck_and_looks_only_inside(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    shape: str, mode: list[str],
) -> None:
    """The last recheck (`_still_absent`: a name that was read as absent is looked at again just before the removal) is
    reached only by a record that names a place that is not there. Here the record names one inside the fence: the
    recheck runs (the test shows that it did, and for which name), looks only inside the fence, and the checkout, which
    nothing uses, is released or swept. A planted outside look there is caught by this guard."""
    from agenttalk import worktree_release

    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    missing = fence / "zz-missing-folder" / "deeper"  # inside the fence, and not there
    _write_guard_record(repo, shape, str(missing))
    guard = _install_guard(monkeypatch, fence)
    rechecked: list[list[str]] = []
    real_recheck = worktree_release._still_absent
    monkeypatch.setattr(
        worktree_release, "_still_absent", lambda seen: rechecked.append(list(seen)) or real_recheck(seen)
    )

    cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert guard.outside == [], f"a path outside the fence was looked at: {guard.outside}"
    names = {os.path.normcase(entry) for seen in rechecked for entry in seen}
    assert os.path.normcase(str(fence / "zz-missing-folder")) in names, (rechecked, text)  # the recheck was reached
    assert not done.exists(), text  # nothing uses the checkout: it is released or swept


LINE_SEPARATOR_NAME = f"a{LINE_SEPARATOR}b"


@pytest.mark.parametrize("mode", [pytest.param(["--release-report"], id="release-report"),
                                  pytest.param(["--release", "{done}"], id="release")])
def test_a_checkout_named_with_a_line_separator_reaches_the_release_walk_and_no_link_is_resolved(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: list[str],
) -> None:
    """Master's text reader cut a name with a line separator short (`a` for `a<U+2028>b`), a path that does not exist,
    and the release preflight refused. The exact reader of round 14 names the checkout correctly, so the preflight
    passes and the release reaches the walk that looks for the other names of the origin's hard-linked files. That walk
    must not resolve a link inside the fence that leads outside (the guard records any such look). The exposure is
    therefore not one that master already had: it came with the exact reader, which stays."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    scratch = fence / "scratch"
    done = scratch / LINE_SEPARATOR_NAME
    repo = _merged_checkout(fence, done, scratch)
    target = tmp_path / "zz-guard-link-target-2028"
    target.mkdir()
    _dir_link(fence / "link-to-outside", target)
    exact = janitor._registered_worktrees_listed(repo)
    if all(path in exact for path in janitor.get_registered_worktrees(repo)):
        pytest.skip("this platform's text reader reads the name correctly: there is no cut-short path to compare")
    objects = [p for p in (fence / "remote.git" / "objects").rglob("*") if p.is_file()]
    if not any(os.stat(p).st_nlink > 1 for p in objects):
        pytest.skip("this file system gave the local origin no hard-linked objects: the walk has nothing to look for")
    walks: list[bool] = []
    real_walk = cli.store_mod.check_second_names
    monkeypatch.setattr(
        cli.store_mod, "check_second_names",
        lambda files, *, follow_links=True: walks.append(follow_links) or real_walk(files, follow_links=follow_links),
    )
    guard = _install_guard(monkeypatch, fence)

    cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    text = capsys.readouterr().out
    assert walks and not any(walks), "the release never reached the walk that follows no link (the hard-linked files)"
    assert guard.outside == [], f"a path outside the fence was looked at: {guard.outside}"
    shown = cli.store_mod.escape_controls(str(done))  # the report writes a line separator out as \u2028
    assert f"WOULD RELEASE {shown}" in text or f"REMOVED {shown}" in text, text


def test_the_guard_lets_a_fence_ancestor_through_only_for_the_checks_that_expect_it(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's P2 on the guard: it let any look at an exact ancestor of the fence through, whatever asked. A
    record that names an ancestor, read by a reader that regressed, would not have been recorded. Now an ancestor is
    looked at freely only by the identity checks of the configured roots (which are expected to) and by the resolution
    of a path inside the fence; the same look from anything else is an outside look, recorded and answered with an
    error."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    parent = fence.parent
    status = os.lstat(parent)
    identities = {parent: (status.st_dev, status.st_ino)}
    guard = _install_guard(monkeypatch, fence)

    assert janitor._ancestor_change_reason(identities) is None  # the expected look: allowed
    assert guard.outside == [], guard.outside
    for place in (parent, tmp_path.parent):  # a record names an ancestor; a reader asks about it
        looks = (os.stat, os.lstat, os.path.exists, os.path.isdir, os.path.realpath, lambda name: Path(name).stat())
        for look in looks:
            try:
                look(place)
            except OSError as exc:
                assert exc.errno == errno.EIO, (place, exc)
            else:
                raise AssertionError(f"a look at the ancestor {place} was let through")
    assert len(guard.outside) == 12, guard.outside


@pytest.mark.parametrize("shape", ["second-name-behind-a-link-out", "second-name-inside-behind-a-link-in"])
def test_the_walk_that_follows_no_link_gives_the_same_verdict_as_the_one_that_does(
    fenced, tmp_path: Path, shape: str
) -> None:
    """The zteam lead's condition on the store.py keyword: the verdict is the same with `follow_links` true and false,
    shown by a test. A hard-linked file whose second name sits under an in-fence link that points outside (refused
    in both: that name is not inside the fence), and one whose second name sits inside the fence, behind an in-fence
    link to another in-fence folder (passed in both: the folder is walked as itself)."""
    fence, _, _ = fenced
    fence = fence.resolve()
    first = fence / "objects-a"
    first.mkdir()
    file = first / "obj"
    file.write_text("same file\n", encoding="utf-8")
    try:
        if shape == "second-name-behind-a-link-out":
            elsewhere = tmp_path / "zz-second-name-outside"
            elsewhere.mkdir()
            os.link(file, elsewhere / "obj2")
            _dir_link(fence / "link-out", elsewhere)
            expected = "refused"
        else:
            second = fence / "objects-b"
            second.mkdir()
            os.link(file, second / "obj2")
            _dir_link(fence / "link-in", second)
            expected = "passed"
    except OSError as exc:
        pytest.skip(f"this file system cannot make the hard link or the folder link: {exc}")

    def verdict(follow: bool) -> str:
        try:
            cli.store_mod.check_second_names([file], follow_links=follow)
        except cli.store_mod.StoreFenceError as exc:
            return f"refused: {exc}"
        return "passed"

    assert verdict(True) == verdict(False)
    assert verdict(False).startswith(expected), verdict(False)


def _network_origin(tmp_path: Path) -> str:
    if os.name == "nt":
        return "\\\\" + _GUARD_HOST + "\\share\\origin"
    return f"//{_GUARD_HOST}/share/origin"


_ORIGINS = {
    "outside": lambda tmp_path: str(tmp_path / "zz-guard-origin-outside" / "origin.git"),
    "network": _network_origin,
}


@pytest.mark.parametrize("mode", [pytest.param(["--release-report"], id="release-report"),
                                  pytest.param(["--release", "{done}"], id="release")])
@pytest.mark.parametrize("origin", list(_ORIGINS))
def test_an_origin_outside_the_fence_is_refused_before_anything_resolves_it(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str, mode: list[str],
) -> None:
    """The release preflight built the forms of the origin's folder with `resolve()` before it asked the fence, so
    an origin outside the fence, or a network path, was resolved (looked at, or contacted) before it was refused. The
    origin as written is compared with the fence first, as a string: outside is refused at once. The spies (every
    resolver and every look, answered with an error, never forwarded) see nothing outside, for the report and the
    release."""
    fence, _, report = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    (tmp_path / "zz-guard-origin-outside").mkdir()
    url = _ORIGINS[origin](tmp_path)
    _git(repo, "remote", "set-url", "origin", url)
    guard = _install_guard(monkeypatch, fence)

    rc = cli.main(["--root", str(repo), "janitor", *_janitor_args(mode, done)])

    assert guard.outside == [], f"the origin outside the fence was looked at: {guard.outside}"
    assert rc == 2
    assert report.read_text(encoding="utf-8").splitlines() == [str(Path(url))]  # refused as written, nothing resolved
    assert (done / ".git").exists()  # nothing was released


def test_a_late_fence_refusal_stops_the_sweep_before_the_prune_and_before_anything_else_runs(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The review bot's P1 and the zteam lead's widening of it. After the preflight, Git's list names a checkout
    outside the fence; the sweep's last look refuses it (`StoreFenceError`). That was read as one failed candidate, and
    the sweep went on: the next candidates, then `git worktree prune`, which reads every registration, the outside one
    too. Now the refusal stops the sweep: one spy test across every step the sweep takes after it (each Git runner,
    the listing, the probes, the commit, the removal, the ancestry check, the prune), none is called; nothing is
    removed; the exit code is 1 and the report says why."""
    from agenttalk import janitor

    fence, _, report = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    leftover = _stale_leftover(fence / "temp")
    stranger = (tmp_path / "outside-checkout").resolve()
    stranger.mkdir()
    calls = _register_outside_after(monkeypatch, stranger)
    looked = _watch_looks_at(monkeypatch, stranger)
    state = {"refused": False}
    after: list[str] = []
    prunes: list[tuple] = []
    real_changed = janitor._checkouts_changed

    def changed(*args, **kwargs):
        try:
            return real_changed(*args, **kwargs)
        except cli.store_mod.StoreFenceError:
            state["refused"] = True
            raise

    monkeypatch.setattr(janitor, "_checkouts_changed", changed)
    for name in ("_run_git", "_run_git_checked", "get_registered_worktrees", "_registered_worktrees_listed",
                 "is_dirty_worktree", "wip_commit_dirty_worktree", "worktree_branch", "remove_plainly",
                 "_ancestor_change_reason"):
        real_step = getattr(janitor, name)

        def step(*args, _name=name, _real=real_step, **kwargs):
            if state["refused"]:
                after.append(_name)
            if _name == "_run_git" and args[1:3] == ("worktree", "prune"):
                prunes.append(args)
            return _real(*args, **kwargs)

        monkeypatch.setattr(janitor, name, step)

    rc = cli.main(["--root", str(repo), "janitor", "--apply"])

    text = capsys.readouterr().out
    assert calls["sweep"] >= 2 and state["refused"], "the late refusal never happened, so nothing was tested"
    assert looked == [], f"the outside checkout was looked at: {looked}"
    assert prunes == [], "git worktree prune ran after the fence refused a registration"
    assert after == [], f"steps that ran after the refusal: {after}"
    assert rc == 1, text
    assert "worktree prune skipped" in text and "store fence refused" in text, text
    assert (done / ".git").exists() and (leftover / "note.txt").exists()  # nothing was removed
    assert str(stranger) in report.read_text(encoding="utf-8").split("\n")


# --- round 18c: the fence's refusal of a record path keeps its type all the way up, so the sweep stops ----------


def _sweep_with_step_spies(
    monkeypatch: pytest.MonkeyPatch, *, real_prune: bool = True
) -> tuple[dict, list[str], list[tuple]]:
    """Wrap every step `apply` takes (each Git runner, the listing, the probes, the commit, the removal, the ancestry
    check) so that a call made after the fence's refusal is noted. Returns (the state, the steps taken after the
    refusal, the prune calls). The refusal is noticed where a record path is fenced (`_fence_record_path` raising a
    `Refused` whose cause is the fence's own error: the translated case) and where the last look reads the list
    (`_checkouts_changed` raising the fence's error itself: the direct case)."""
    from agenttalk import janitor, worktree_release

    state = {"refused": False}
    after: list[str] = []
    prunes: list[tuple] = []
    real_changed, real_record = janitor._checkouts_changed, worktree_release._fence_record_path

    def changed(*args, **kwargs):
        try:
            return real_changed(*args, **kwargs)
        except cli.store_mod.StoreFenceError:
            state["refused"] = True
            raise

    def record(*args, **kwargs):
        try:
            return real_record(*args, **kwargs)
        except worktree_release.Refused as exc:
            fence_error = isinstance(exc.__cause__ or exc.__context__, cli.store_mod.StoreFenceError)
            if fence_error or "outside the fence" in str(exc):
                state["refused"] = True  # the fence refused this path, whatever the refusal is called now
            raise

    real_apply = janitor.apply

    def apply(*args, **kwargs):
        state["refused"] = False  # the report that came first reads the records too; only what apply does is counted
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(janitor, "apply", apply)
    monkeypatch.setattr(janitor, "_checkouts_changed", changed)
    monkeypatch.setattr(worktree_release, "_fence_record_path", record)
    for name in ("_run_git", "_run_git_checked", "get_registered_worktrees", "_registered_worktrees_listed",
                 "is_dirty_worktree", "wip_commit_dirty_worktree", "worktree_branch", "remove_plainly",
                 "_ancestor_change_reason"):
        real_step = getattr(janitor, name)

        def step(*args, _name=name, _real=real_step, **kwargs):
            if state["refused"]:
                after.append(_name)
            if _name == "_run_git" and args[1:3] == ("worktree", "prune"):
                prunes.append(args)
                if not real_prune:
                    return ""  # recorded, not run: the call is what is shown, and nothing real is pruned
            return _real(*args, **kwargs)

        monkeypatch.setattr(janitor, name, step)
    return state, after, prunes


@pytest.mark.parametrize("how", ["translated", "translated-last-look", "late-inventory"])
def test_a_refusal_by_the_fence_stops_the_sweep_however_it_reaches_apply(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, how: str
) -> None:
    """The fence refuses a path in the middle of a sweep in two ways. DIRECT: Git's list names a checkout outside the
    fence (the fence's own error comes out of the last look). TRANSLATED: an activity record names a path inside the
    fence that is a link leading outside; the record reader fences it and turns the fence's error into a `Refused`
    (its type is `FenceRefused`), at the first look or, for "translated-last-look", only at the last look (the record
    appears just before the re-read under the lane writers' lock). The translated cases used to be read as an ordinary
    kept checkout: the sweep went on and ran `git worktree prune`, which reads every registration. All stop it: the
    steps after the refusal (each Git runner, the listing, the probes, the commit, the removal, the ancestry check,
    the prune) are not called, nothing is removed, the report says why and the exit code is 1."""
    from agenttalk import worktree_release

    fence, _, report = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    leftover = _stale_leftover(fence / "temp")
    stranger = (tmp_path / "outside-checkout").resolve()
    stranger.mkdir()
    if how == "late-inventory":
        calls = _register_outside_after(monkeypatch, stranger)
    else:
        calls = {"sweep": 2}
        target = tmp_path / "zz-link-target"
        target.mkdir()
        _dir_link(fence / "link-to-outside", target)
        value = str(fence / "link-to-outside" / "x")
        if how == "translated":
            _write_guard_record(repo, "lane-every-field", value)
        else:
            real_reread = worktree_release.RemovalGuard.reread

            def late(self):
                _write_guard_record(repo, "lane-every-field", value)
                return real_reread(self)

            monkeypatch.setattr(worktree_release.RemovalGuard, "reread", late)
    state, after, prunes = _sweep_with_step_spies(monkeypatch)

    rc = cli.main(["--root", str(repo), "janitor", "--apply"])

    text = capsys.readouterr().out
    assert calls["sweep"] >= 2 and state["refused"], "the refusal never happened, so nothing was tested"
    assert prunes == [], "git worktree prune ran after the fence refused a path"
    assert after == [], f"steps that ran after the refusal: {after}"
    assert rc == 1, text
    assert "worktree prune skipped" in text and "store fence refused" in text, text
    assert (done / ".git").exists(), text  # the checkout was kept
    if how == "late-inventory":
        assert (leftover / "note.txt").exists()  # nor was any later candidate looked at, let alone removed
        assert str(stranger) in report.read_text(encoding="utf-8").split("\n")


def test_an_ordinary_in_use_refusal_still_just_keeps_the_checkout_and_the_sweep_goes_on(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The control: an active lane names the checkout (a path inside the fence, nothing refused by the fence). The
    checkout is kept and said to be in use, the sweep does not stop: the stale leftover is removed, the prune runs, and
    the exit code is 0 (a kept checkout is not a failure)."""
    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    leftover = _stale_leftover(fence / "temp")
    lanes_file = repo / ".agenttalk" / "state" / "lanes.json"
    lanes_file.parent.mkdir(parents=True, exist_ok=True)
    lanes_file.write_text(json.dumps({"lanes": {"one": {"status": "active", "worktree_path": str(done)}}}),
                          encoding="utf-8")
    state, after, prunes = _sweep_with_step_spies(monkeypatch)

    rc = cli.main(["--root", str(repo), "janitor", "--apply"])

    text = capsys.readouterr().out
    assert not state["refused"]
    assert (done / ".git").exists() and "REFUSED (kept)" in text, text
    assert not leftover.exists(), text  # the sweep went on to the next candidate
    assert prunes, "the prune did not run"
    assert rc == 0 and "store fence refused" not in text, text


@pytest.mark.parametrize("late", ["outside-git-absent", "outside-git-present", "then-a-later-candidate", "none"])
def test_a_registration_made_after_the_preflight_for_a_refused_record_path_stops_the_sweep_before_the_real_prune(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, late: str
) -> None:
    """An activity record names a local folder outside the fence, so its checkout is kept from the text alone (no look).
    After the preflight and the preview, at the start of the real `--apply`, Git gets a registration of that same
    folder (`.git/worktrees/late-registration`, its `gitdir` naming the folder's `.git`, an old time on it). Git's
    REAL prune reads every registration and looks at the path it names: with the folder's `.git` absent it removes the
    entry, with it present it keeps it; either way it has looked outside the fence. So the registrations prune would
    read are fenced, from Git's own files and as written, before the prune: the sweep stops, the prune does not run (the
    entry is still there), nothing outside is touched, and the exit code is 1. "then-a-later-candidate": a stale
    leftover comes after the kept checkout; its last look (Git's list, which looks at every registration too) is
    fenced the same way first, so the sweep stops there and the leftover is kept. "none", the control: the same kept
    checkout with no late registration; the sweep goes on, the real prune runs, and the exit code is 0."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    leftover = _stale_leftover(fence / "temp") if late == "then-a-later-candidate" else None
    refused = (tmp_path / "zz-refused-folder").resolve()  # beside the fence, inside this test's own folder
    refused.mkdir()
    if late == "outside-git-present":
        (refused / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    _write_guard_record(repo, "lane-every-field", str(refused))
    admin = repo / ".git" / "worktrees" / "late-registration"
    real_apply = janitor.apply

    def apply(*args, **kwargs):
        if late != "none":
            _write_late_registration(admin, refused)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(janitor, "apply", apply)
    state, _, prunes = _sweep_with_step_spies(monkeypatch)  # the prune is the real one
    listed_with_it: list[tuple] = []  # Git's list of checkouts, asked for while the late registration exists
    spied_run = janitor._run_git_checked

    def run(repo_arg, *args, **kwargs):
        if args[:2] == ("worktree", "list") and admin.exists():
            listed_with_it.append(args)
        return spied_run(repo_arg, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", run)

    rc = cli.main(["--root", str(repo), "janitor", "--apply"])

    text = capsys.readouterr().out
    assert state["refused"], "the record path was never refused, so nothing was tested"
    assert (done / ".git").exists() and "REFUSED (kept)" in text, text
    assert sorted(p.name for p in refused.iterdir()) == ([".git"] if late == "outside-git-present" else []), text
    if late == "none":
        assert prunes and rc == 0 and "store fence refused" not in text, text
        return
    assert admin.is_dir() and (admin / "gitdir").exists(), "the registration was pruned: Git looked at it"
    assert rc == 1, text
    assert prunes == [], "git worktree prune ran, and read a registration outside the fence"
    assert listed_with_it == [], "Git listed the checkouts, and so looked at the registration outside the fence"
    assert "worktree prune skipped" in text and "store fence refused" in text, text
    assert str(refused) in text, text
    if leftover is not None:
        assert (leftover / "note.txt").exists(), text  # the later candidate was not removed: the sweep stopped


def _write_late_registration(admin: Path, folder: Path) -> None:
    """A registration of `folder` as Git writes one (`gitdir`, `HEAD`, `commondir`), with an old time on `gitdir`."""
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{folder / '.git'}\n", encoding="utf-8")
    (admin / "HEAD").write_text("ref: refs/heads/late-registration\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    os.utime(admin / "gitdir", (1, 1))


def _git_asked_for_every_registration(monkeypatch: pytest.MonkeyPatch, admin: Path) -> list[tuple]:
    """Every Git call, by any runner of the sweep or of release, that reads every registration (`worktree list`,
    `worktree prune`, `worktree remove`), made while the registration `admin` exists."""
    from agenttalk import janitor, worktree_release

    asked: list[tuple] = []
    for owner, name in ((janitor, "_run_git_checked"), (worktree_release, "_git")):
        real = getattr(owner, name)

        def spy(repo, *args, _real=real, **kwargs):
            if args[:1] == ("worktree",) and args[1:2] in (("list",), ("prune",), ("remove",)) and admin.exists():
                asked.append(args)
            return _real(repo, *args, **kwargs)

        monkeypatch.setattr(owner, name, spy)
    return asked


def test_a_registration_made_after_the_preflight_is_fenced_before_release_asks_git_about_the_registrations(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Release reads Git's list of checkouts again inside the release (the main-repository check, each checkout's own
    check) and asks Git to remove the checkout; each of these reads every registration and looks at the path it names.
    A registration of a folder outside the fence, made after the preflight (at the start of the release itself), is
    refused from Git's own files before Git is asked anything about the registrations: no list, no removal, the
    checkout is kept, exit 1."""
    from agenttalk import worktree_release

    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    outside = (tmp_path / "zz-late-release").resolve()  # beside the fence, inside this test's own folder
    outside.mkdir()
    admin = repo / ".git" / "worktrees" / "late-registration"
    real_release = worktree_release.release

    def release(*args, **kwargs):
        _write_late_registration(admin, outside)
        return real_release(*args, **kwargs)

    monkeypatch.setattr(worktree_release, "release", release)
    asked = _git_asked_for_every_registration(monkeypatch, admin)

    rc = cli.main(["--root", str(repo), "janitor", "--release", str(done)])

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert admin.is_dir(), "the registration was never made, so nothing was tested"
    assert asked == [], f"Git was asked about every registration, the outside one too: {asked}"
    assert rc == 1, text
    assert (done / ".git").exists() and "REMOVED" not in text, text
    assert str(outside) in text, text
    assert list(outside.iterdir()) == [], text


def _file_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError as exc:  # Windows without the privilege to make a file link
        pytest.skip(f"this system cannot make a file link: {exc}")


@pytest.mark.parametrize("where", ["worktrees-folder", "entry-folder", "gitdir-file", "commondir-file"])
def test_the_registration_check_fences_each_git_file_before_it_looks_through_a_link_that_leads_outside(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """The check of Git's registration files reads Git's own folder: `commondir`, the `worktrees` folder, each entry
    folder and each entry's `gitdir` file. Any of them can be a link (a junction on Windows for a folder) that leads to
    a folder beside the fence. Each is fenced, by the fence's own check, BEFORE anything looks at it or through it: the
    check is refused and the guard over every file-system look saw no look outside the fence."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    git_dir = repo / ".git"
    away = (tmp_path / "zz-git-files-away").resolve()  # beside the fence, inside this test's own folder
    away.mkdir()
    if where == "worktrees-folder":
        (git_dir / "worktrees").rename(away / "worktrees")
        _dir_link(git_dir / "worktrees", away / "worktrees")
    elif where == "entry-folder":
        (away / "entry").mkdir()
        (away / "entry" / "gitdir").write_text(f"{done / '.git'}\n", encoding="utf-8")
        _dir_link(git_dir / "worktrees" / "zz-entry", away / "entry")
    elif where == "gitdir-file":
        (away / "gitdir").write_text(f"{done / '.git'}\n", encoding="utf-8")
        (git_dir / "worktrees" / "zz-entry").mkdir()
        _file_link(git_dir / "worktrees" / "zz-entry" / "gitdir", away / "gitdir")
    else:
        (away / "commondir").write_text(".\n", encoding="utf-8")
        _file_link(git_dir / "commondir", away / "commondir")
    guard = _install_guard(monkeypatch, fence)

    with pytest.raises(cli.store_mod.StoreFenceError):
        janitor._fence_registrations(repo)

    assert guard.outside == [], f"a look outside the fence: {guard.outside}"


def test_under_the_store_fence_a_failed_strict_list_is_not_replaced_by_the_old_git_list(
    fenced, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report keeps a best-effort list of checkouts for its counts when the strict list fails. Under the store
    fence it does not ask Git for it: that list was never fenced, and Git's listing looks at every registration."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    repo, _ = _swept_checkout(fence)
    cfg = janitor.JanitorConfig.load(repo)
    asked = _git_asked_for_every_registration(monkeypatch, repo / ".git")

    report = janitor.build_report(cfg, (None, "the strict list failed (test)"))

    assert asked == [], asked
    assert report.registered_worktrees == [] and not report.registered_strict


def test_the_guard_notes_an_outside_path_handed_to_a_git_runner_as_an_argument(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Condition (2): the path a record names is never in any Git runner's arguments. The guard's runner spies check
    the repository AND every argument: an absolute path outside the fence (a UNC one included) is recorded and answered
    with an error; a path inside is let through. No Git is started."""
    from agenttalk import janitor

    fence, _, _ = fenced
    fence = fence.resolve()
    outside = _guard_value("outside", tmp_path, fence)  # made before the guard: it would answer a look at the folder
    network = _guard_value("network", tmp_path, fence)
    guard = _install_guard(monkeypatch, fence)

    for path in (outside, network):
        with pytest.raises(OSError) as raised:
            janitor._run_git(fence, "worktree", "remove", "--", path)
        assert raised.value.errno == errno.EIO
    assert [name for name, _ in guard.outside] == ["agenttalk.janitor._run_git argument"] * 2, guard.outside
    assert [text for _, text in guard.outside] == [outside, network], guard.outside


class _PathLike:
    """Anything with `__fspath__` that is not a `str` or a `Path`: a Git runner is handed such an argument too."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __fspath__(self) -> str:
        return self.text


def _fake_git_runner(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Put a fake in place of `janitor._run_git` BEFORE the guard is installed, so that the guard's spy wraps the fake:
    what reaches the fake was let through. No Git is started, and nothing real is looked at."""
    from agenttalk import janitor

    forwarded: list[tuple] = []
    monkeypatch.setattr(janitor, "_run_git", lambda repo, *args, **kwargs: forwarded.append(args) or "")
    return forwarded


# The forms in which a path reaches a Git runner as an argument, each built around the text `where`.
_GIT_ARGUMENT_FORMS = {
    "a-path-object": lambda where: ("worktree", "remove", Path(where)),
    "an-os-pathlike": lambda where: ("worktree", "remove", _PathLike(where)),
    "a-plain-string": lambda where: ("worktree", "remove", "--", where),
    "git-dir-equals": lambda where: (f"--git-dir={where}", "status"),
    "work-tree-equals": lambda where: (f"--work-tree={where}", "status"),
    "any-option-equals": lambda where: (f"--some-option={where}", "status"),
    "dash-C-then-path": lambda where: ("-C", where, "status"),
    "git-dir-then-path": lambda where: ("--git-dir", where, "status"),
}


def _try_git_forms(monkeypatch: pytest.MonkeyPatch, fence: Path, forms: dict, make_where) -> tuple:
    """(the guard, what the fake runner received, the refusals) for each form in `forms` handed to the runner, with the
    runner's repository `fence/repo`: a refusal is the guard's own error (`EIO`), which the real call never gives."""
    from agenttalk import janitor

    forwarded = _fake_git_runner(monkeypatch)
    guard = _install_guard(monkeypatch, fence)
    refused: list[str] = []
    for name, build in forms.items():
        try:
            janitor._run_git(fence / "repo", *build(make_where(name)))
        except OSError as exc:
            assert exc.errno == errno.EIO, (name, exc)
            refused.append(name)
    return guard, forwarded, refused


def test_the_guard_notes_a_path_object_handed_to_a_git_runner(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A path object (`pathlib.Path`, any `os.PathLike`) outside the fence, as an argument, is read with
    `os.fspath`: recorded, answered with an error and never forwarded to the runner."""
    fence, _, _ = fenced
    fence = fence.resolve()
    outside = str(tmp_path.resolve() / "zz-outside-object")
    forms = {name: _GIT_ARGUMENT_FORMS[name] for name in ("a-path-object", "an-os-pathlike")}

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: outside)

    assert refused == list(forms) and forwarded == [], (refused, forwarded)
    assert [text for _, text in guard.outside] == [outside] * 2, guard.outside


def test_the_guard_notes_a_relative_path_that_leads_outside_the_fence_in_a_git_runners_arguments(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A relative argument is read against the runner's repository, as text (nothing is looked at): from
    `fence/repo`, `../../zz-outside-relative` is a folder beside the fence. Recorded, answered with an error and never
    forwarded, as a string, as a path object and inside an option."""
    fence, _, _ = fenced
    fence = fence.resolve()
    relative = os.path.join("..", "..", "zz-outside-relative")
    forms = {
        "a-plain-string": _GIT_ARGUMENT_FORMS["a-plain-string"],
        "a-path-object": _GIT_ARGUMENT_FORMS["a-path-object"],
        "git-dir-equals": _GIT_ARGUMENT_FORMS["git-dir-equals"],
        "dash-C-then-path": _GIT_ARGUMENT_FORMS["dash-C-then-path"],
    }

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: relative)

    assert refused == list(forms) and forwarded == [], (refused, forwarded)
    assert len(guard.outside) == len(forms), guard.outside


def test_the_guard_notes_an_outside_path_inside_a_git_option(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) A path outside the fence inside an option: `--git-dir=P`, `--work-tree=P`, any `--opt=P`, `-C P` and
    `--git-dir P`. The value is split out and judged like any other argument."""
    fence, _, _ = fenced
    fence = fence.resolve()
    outside = str(tmp_path.resolve() / "zz-outside-option")
    forms = {
        name: _GIT_ARGUMENT_FORMS[name]
        for name in ("git-dir-equals", "work-tree-equals", "any-option-equals", "dash-C-then-path", "git-dir-then-path")
    }

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: outside)

    assert refused == list(forms) and forwarded == [], (refused, forwarded)
    assert [text for _, text in guard.outside] == [outside] * len(forms), guard.outside


def test_the_guard_notes_an_outside_path_in_a_config_value_a_combined_dash_c_and_after_a_change_of_folder(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(1) `-c core.worktree=P`: the value of a configuration setting is split out and judged. (2) `-CP`, written as one
    argument, is a change of folder like `-C P`. (3) Git applies each `-C` in order, so a relative argument after it
    is read against the NEW folder: from `fence/repo`, `-C <fence> --git-dir=../P` names a folder beside the fence.
    Each is recorded, answered with an error and never forwarded."""
    fence, _, _ = fenced
    fence = fence.resolve()
    outside = str(tmp_path.resolve() / "zz-outside-form")
    beside = "zz-outside-form"  # tmp_path/fence is the fence, so ../<beside> from the fence is the folder above
    assert (fence.parent / beside) == Path(outside), "the fence is expected one level below tmp_path"
    forms = {
        "config-value": lambda where: ("-c", f"core.worktree={where}", "status"),
        "dash-C-combined": lambda where: (f"-C{where}", "status"),
        "relative-after-dash-C": lambda where: ("-C", str(fence), f"--git-dir=../{beside}", "status"),
        "relative-after-dash-C-combined": lambda where: (f"-C{fence}", "worktree", "remove", "--", f"../{beside}"),
    }

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: outside)

    assert refused == list(forms) and forwarded == [], (refused, forwarded)
    assert len(guard.outside) == len(forms), guard.outside


def test_the_guard_notes_an_unknown_git_option_that_carries_a_value(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: an option the guard does not know, with a value, is recorded whatever the value (the guard cannot
    tell what Git reads from it), as `--opt=V` before or after the command and as a short option with its value
    joined (`-OV`). The same holds for an option the guard knows to take no value, given one anyway, and for an
    unknown option with no value (the guard cannot tell whether Git reads the next argument as its value)."""
    fence, _, _ = fenced
    fence = fence.resolve()
    outside = str(tmp_path.resolve() / "zz-outside-unknown")
    inside = str(fence / "inside")
    forms = {
        "unknown-before-the-command-inside": lambda where: (f"--some-option={inside}", "status"),
        "unknown-after-the-command-inside": lambda where: ("status", f"--some-option={inside}"),
        "unknown-short-joined-outside": lambda where: ("status", f"-O{where}"),
        "unknown-short-joined-inside": lambda where: ("status", f"-O{inside}"),
        "a-switch-given-a-value": lambda where: ("worktree", "list", f"-z={inside}"),
        "dash-C-after-the-command": lambda where: ("commit", "-C", inside),
        "unknown-with-no-value": lambda where: ("status", "--some-switch"),
        "unknown-then-an-outside-path": lambda where: ("status", "--some-option", where),
    }

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: outside)

    assert refused == list(forms) and forwarded == [], (refused, forwarded)
    assert len(guard.outside) == len(forms), guard.outside


def test_the_guard_notes_a_git_argument_it_cannot_place(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: an argument that is neither provably a plain word nor provably a path inside the fence is
    recorded: an address (`file:///...`, `host:path`), a home folder Git expands (`~/...`), a relative path in a
    configuration value or after `--git-path` (Git reads those against its own folder, not the working one), a drive
    with no folder after it (Windows), and an argument that is not text at all."""
    fence, _, _ = fenced
    fence = fence.resolve()
    forms = {
        "a-file-address": lambda where: ("ls-remote", "file:///zz-outside-address"),
        "a-host-and-path": lambda where: ("ls-remote", "zz-host:zz/outside"),
        "a-home-folder": lambda where: ("worktree", "remove", "--", "~/zz-outside-home"),
        "a-relative-config-value": lambda where: ("-c", "core.worktree=sub/folder", "status"),
        "a-relative-git-path": lambda where: ("rev-parse", "--git-path", os.path.join("..", "zz-outside-git-path")),
        "not-text": lambda where: ("worktree", "remove", 858),
    }
    if os.name == "nt":
        forms["a-drive-with-no-folder"] = lambda where: ("worktree", "remove", "--", "Z:zz-outside-drive")

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: "")

    assert refused == list(forms) and forwarded == [], (refused, forwarded)
    assert len(guard.outside) == len(forms), guard.outside


@pytest.mark.parametrize(
    "kind", ["absolute", "relative-below", "relative-beside"]
)
def test_the_guard_lets_an_inside_path_through_in_every_form_of_a_git_runners_arguments(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The control: the same forms with a path INSIDE the fence (absolute, relative below the repository, relative
    beside it that stays inside the fence) are forwarded to the runner and not recorded, so the wider check makes no
    false hit: also as `-CP` written as one argument, after a change of folder (`-C sub`, so `..` from there is the
    repository), after `ls-remote --get-url`, beside the known switches and a plain `--git-path` word, and, for an
    absolute path, as a configuration value (`-c core.worktree=P`). An unknown option
    (`--some-option=P`) is not in the control: it is recorded whatever its value (see the test of unknown options)."""
    fence, _, _ = fenced
    fence = fence.resolve()
    place = {
        "absolute": str(fence / "inside"),
        "relative-below": os.path.join("sub", "folder"),
        "relative-beside": os.path.join("..", "inside-sibling"),
    }[kind]
    forms = {name: build for name, build in _GIT_ARGUMENT_FORMS.items() if name != "any-option-equals"}
    forms["dash-C-combined"] = lambda where: (f"-C{where}", "status")
    forms["after-dash-C"] = lambda where: ("-C", "sub", "worktree", "remove", "--", where)
    forms["after-dash-C-combined"] = lambda where: ("-Csub", "--git-dir", where, "status")
    forms["known-options-and-words"] = lambda where: (
        "-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=all", "-z", "--", where)
    forms["git-path-a-plain-word"] = lambda where: ("rev-parse", "--git-path", "objects")
    forms["get-url"] = lambda where: ("ls-remote", "--get-url", where)
    if kind == "absolute":
        forms["config-value"] = lambda where: ("-c", f"core.worktree={where}", "status")

    guard, forwarded, refused = _try_git_forms(monkeypatch, fence, forms, lambda name: place)

    assert refused == [] and guard.outside == [], (place, refused, guard.outside)
    assert len(forwarded) == len(forms), (place, forwarded)


@pytest.mark.parametrize("route", ["release", "apply"])
def test_under_the_store_fence_a_unc_record_is_kept_and_never_looked_at_on_release_and_apply(
    fenced, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, route: str
) -> None:
    """Conditions (1), (2) and (4), with the suite's fence on. An activity record names a UNC (network) path. It is
    refused from its text alone: the checkout is KEPT on both routes, and the path is never looked at. ONE guard watches
    the whole run (every file-system look, and every Git runner's repository and arguments): it records any look at a
    path outside the fence and answers it with an error, so the test fails if one happens, on release and apply alike.
    RELEASE: exit 1, `KEPT <checkout>: ... the record names a path outside the fence ... the checkout was kept`.
    APPLY: the checkout is `REFUSED (kept)`, the sweep GOES ON (it does not stop): the ancestry check, the prune (shown
    as called, not run) and the list of checkouts after it all run, and none of them looks at the path; exit 0."""
    fence, _, _ = fenced
    fence = fence.resolve()
    repo, done = _swept_checkout(fence)
    _write_guard_record(repo, "lane-every-field", _guard_value("network", tmp_path, fence))
    guard = _install_guard(monkeypatch, fence)
    state, after, prunes = _sweep_with_step_spies(monkeypatch, real_prune=False)

    rc = cli.main(["--root", str(repo), "janitor", *(["--release", str(done)] if route == "release" else ["--apply"])])

    text = capsys.readouterr().out
    assert guard.outside == [], f"the path was looked at: {guard.outside}"
    assert (done / ".git").exists() and (done / "source.txt").exists(), text  # kept, not released
    assert "store fence refused" not in text and "REMOVED" not in text, text
    if route == "release":
        assert rc == 1, text
        assert f"KEPT {done}: " in text and "the record names a path outside the fence" in text, text
        assert "the checkout was kept" in text, text
    else:
        assert state["refused"], "the refusal never happened, so nothing was tested"
        assert "REFUSED (kept)" in text and "the record names a path outside the fence" in text, text
        assert rc == 0, text
        # The sweep went on: the steps after the refusal really ran, and none of them looked at the path.
        assert prunes, "the prune was not called: the sweep stopped"
        assert {"_ancestor_change_reason", "get_registered_worktrees"} <= set(after), after
