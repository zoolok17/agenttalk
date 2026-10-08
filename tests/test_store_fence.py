"""The store fence: agenttalk refuses every message store outside AGENTTALK_STORE_FENCE,
and the test suite fences itself to pytest's temporary folder."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


def test_a_test_that_reaches_outside_its_folder_fails_even_through_a_subprocess(tmp_path: Path) -> None:
    """The suite's own guard, run on a small suite of its own: one test opens a store
    outside its temporary folder and swallows the error, another starts a subprocess that
    does and expects the refusal. Both fail; the test that stays inside passes."""
    outside = tmp_path / "outside"
    Store(outside).init(["lead", "worker"])
    before = _snapshot(outside)
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (inner / "conftest.py").write_text(
        "from _store_fence import _no_store_outside_the_test_folder, _store_fence  # noqa: F401\n",
        encoding="utf-8")
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
    env = _env(PYTHONPATH=os.pathsep.join([str(SRC), str(TESTS)]))
    run = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "test_inner.py", "-q", "-p", "no:cacheprovider",
         "-c", "pytest.ini", "--rootdir", str(inner), "--confcutdir", str(inner),
         "--basetemp", str(tmp_path / "inner-bt")],
        cwd=inner, env=env, capture_output=True, text=True, timeout=300,
    )
    out = run.stdout + run.stderr
    assert run.returncode == 1, out
    assert "ERROR test_inner.py::test_swallows_the_refusal" in out, out
    assert "ERROR test_inner.py::test_a_subprocess_reaches_outside" in out, out
    assert "ERROR test_inner.py::test_stays_inside" not in out and "2 errors" in out, out
    assert _snapshot(outside) == before


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


def _inner_suite(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess, bool]:
    """Run `body` as a suite of its own under this guard; (the run, outside store unchanged)."""
    outside = tmp_path / "outside"
    Store(outside).init(["lead", "worker"])
    before = _snapshot(outside)
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (inner / "conftest.py").write_text(
        "from _store_fence import (  # noqa: F401\n"
        "    _no_store_outside_the_test_folder, _store_fence, pytest_configure, pytest_sessionfinish)\n",
        encoding="utf-8")
    (inner / "test_inner.py").write_text(
        "import pytest\nfrom agenttalk.store import Store\n"
        f"OUTSIDE = {str(outside)!r}\n" + textwrap.dedent(body), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "test_inner.py", "-q", "-p", "no:cacheprovider",
         "-c", "pytest.ini", "--rootdir", str(inner), "--confcutdir", str(inner),
         "--basetemp", str(tmp_path / "inner-bt")],
        cwd=inner, env=_env(PYTHONPATH=os.pathsep.join([str(SRC), str(TESTS)])),
        capture_output=True, text=True, timeout=300,
    )
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
def test_a_refusal_outside_any_test_fails_the_run(tmp_path: Path, body: str) -> None:
    """Finding 2: the fence is in place while modules are collected, and a refusal that no
    test saw (collection, a session fixture's setup or its teardown) fails the whole run."""
    run, unchanged = _inner_suite(tmp_path, body)
    out = run.stdout + run.stderr
    assert unchanged, out
    assert run.returncode != 0 and "this run reached a message store outside" in out, out


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
