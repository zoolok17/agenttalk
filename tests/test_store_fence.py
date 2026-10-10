"""The store fence: agenttalk refuses every message store outside AGENTTALK_STORE_FENCE,
and the test suite fences itself to pytest's temporary folder."""

from __future__ import annotations

import json
import multiprocessing
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

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
                                     "...acceptance-write.lock.{token}.prepare.{token}.unlink"])
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
    monkeypatch.setattr(janitor, "_worktrees_discovery_ok", lambda repo: (True, ""))
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
