"""#744: a backup snapshot must not fail, keep a stray lock helper, or miss a file that vanishes while it is copied.

The store's lock code gives a file a private helper name for a moment (".<32 hex>.prepare" and
".<32 hex>.unlink"); a backup that listed it then found it gone failed, or kept the junk name. The
files `_publish_text_no_replace` writes without a lock (launch requests, lane delivery records) must
be COPIED, never linked, because the writer checks right after publishing that the file has exactly
one name. And a folder or file that cannot be listed must start a fresh attempt or refuse, never
give a shorter list.

Nothing here needs a particular Python version: the walker calls os.scandir itself, so the failure
tests replace os.scandir and behave the same on 3.10 and 3.14.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import shutil
from pathlib import Path

import pytest

from agenttalk import cli, recovery
from agenttalk import store as store_mod
from agenttalk.store import STORE_FENCE_ENV, Store

H32 = "0123456789abcdef" * 2
PREPARE = f".{H32}.prepare"
UNLINK = f".{H32}.unlink"
COPY_ONLY_FILES = [
    "state/launch-requests/lr-1.json",
    "lane-deliveries/l1-abc.json",
    "lane-deliveries/.prepared/l1-abc.prepared.json",
]


def _store(root: Path) -> Store:
    s = Store(root)
    s.init(["alpha", "beta"])
    return s


def _put(s: Store, rel: str, text: str = "x") -> Path:
    p = s.dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _links(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Let the backup really hard-link (the test fence turns that off), or skip if this disk cannot."""
    monkeypatch.delenv(STORE_FENCE_ENV, raising=False)
    (tmp_path / "probe").write_bytes(b"x")
    try:
        os.link(tmp_path / "probe", tmp_path / "probe.link")
    except OSError:
        pytest.skip("this filesystem has no hard links")


def _copies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "_probe_hardlink_support", lambda *_a, **_kw: False)


def _no_snapshot(dest: Path) -> None:
    """Nothing completed and nothing half-made is left behind."""
    assert not list(dest.rglob("manifest.json"))
    assert not list(dest.rglob(".tmp-*"))


def _locks_are_free(s: Store) -> None:
    with s._message_publication_lock():
        pass
    s.send(sender="alpha", recipient="beta", body="after the backup")


def _restore_and_take_locks(result: recovery.BackupResult, where: Path) -> Store:
    """Restore a snapshot by hand into a fresh store (there is no restore command yet), open it and take its
    locks (a snapshot that kept a lock helper or a second name could not). Only files are in a snapshot, so a
    folder that was empty in the store, such as messages/, is not there; sending needs it, locks do not."""
    shutil.copytree(result.destination, where / ".agenttalk", ignore=shutil.ignore_patterns("manifest.json"))
    s = Store(where)
    assert s.initialized()
    with s._message_publication_lock():
        pass
    with s._config_lock(), s._retirement_lock():
        pass
    return s


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _after_first_listing(monkeypatch: pytest.MonkeyPatch, s: Store, change) -> list[int]:
    """Run `change()` once, right after the first listing of the store: the window a mover can use."""
    real, seen = recovery._list_files, []

    def listing(root: Path):
        files = real(root)
        if root == s.dir:
            seen.append(1)
            if len(seen) == 1:
                change()
        return files

    monkeypatch.setattr(recovery, "_list_files", listing)
    return seen


# --------------------------------------------------------------------- the helper names


@pytest.mark.parametrize(
    "name",
    [
        PREPARE,
        UNLINK,
        f".events.json.{H32}.prepare",
        f".a.b.{H32}.unlink",
    ],
)
def test_every_private_helper_name_the_lock_code_makes_is_a_lock_artifact(name: str) -> None:
    assert store_mod.is_lock_or_guard_artifact(name)


@pytest.mark.parametrize(
    "name",
    [
        "x.prepare",
        "x.unlink",
        ".abc.prepare",
        f"{H32}.prepare",
        f".{H32}.prepared",
        f".{H32}.unlinked",
        f".{H32}.prepare.bak",
        f".{H32[:-1]}.prepare",
        f".{H32.upper()}.prepare",
        f".{H32}.prepare\n",
        "events.json",
        "prepare",
    ],
)
def test_ordinary_names_that_only_look_like_a_helper_are_not_lock_artifacts(name: str) -> None:
    assert not store_mod.is_lock_or_guard_artifact(name)


@pytest.mark.parametrize("mode", ["link", "copy"])
def test_backup_leaves_helper_names_out_and_deletes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    s = _store(tmp_path / "project")
    _links(monkeypatch, tmp_path) if mode == "link" else _copies(monkeypatch)
    helpers = [_put(s, f"extra/{PREPARE}"), _put(s, f"extra/{UNLINK}"), _put(s, f"extra/.big.{H32}.prepare")]
    keep = [_put(s, "extra/data.prepare"), _put(s, "extra/.abc.unlink")]

    result = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    for p in helpers:
        assert f"extra/{p.name}" not in result.files
        assert p.exists()  # a private leftover is left alone, never deleted
    for p in keep:
        assert f"extra/{p.name}" in result.files
    restored = _restore_and_take_locks(result, tmp_path / "restored")
    assert not [f for f in restored.dir.rglob("*") if store_mod._LOCK_HELPER_NAME.fullmatch(f.name)]


# ------------------------------------------------------------------ a file that vanishes


@pytest.mark.parametrize("mode", ["link", "copy"])
def test_a_file_moved_during_the_listing_gives_a_fresh_pass_not_a_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    s = _store(tmp_path / "project")
    _links(monkeypatch, tmp_path) if mode == "link" else _copies(monkeypatch)
    old = _put(s, "extra/a.txt", "payload")
    seen = _after_first_listing(monkeypatch, s, lambda: old.replace(old.with_name("b.txt")))

    result = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert len(seen) == 2  # the store was listed again
    assert "extra/a.txt" not in result.files
    assert (result.destination / "extra" / "b.txt").read_text(encoding="utf-8") == "payload"


def test_an_archived_launch_request_is_kept_in_one_place_or_the_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path / "project")
    _links(monkeypatch, tmp_path)
    s.write_launch_request({"request_id": "lr-1", "note": "keep me"})
    live = s.launch_requests_dir / "lr-1.json"

    def archive() -> None:
        s.launch_requests_archive_dir.mkdir(parents=True, exist_ok=True)
        live.replace(s.launch_requests_archive_dir / "lr-1.json")

    _after_first_listing(monkeypatch, s, archive)

    result = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    held = [r for r in result.files if r.endswith("lr-1.json")]
    assert held == ["state/launch-requests/archive/lr-1.json"]
    assert "keep me" in (result.destination / held[0]).read_text(encoding="utf-8")


def test_a_fresh_pass_starts_from_an_empty_staging_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    first, second, third = (_put(s, f"extra/a{i}.txt") for i in (1, 2, 3))
    real_copy, state = shutil.copy2, {"done": False}

    def copy2(src, dst, *a, **kw):
        if Path(src) == second and not state["done"]:
            state["done"] = True
            first.unlink()  # copied a moment ago, gone now
            second.unlink()  # and this one vanishes: the pass is thrown away
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(recovery.shutil, "copy2", copy2)

    result = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert state["done"]
    assert "extra/a1.txt" not in result.files and not (result.destination / "extra" / "a1.txt").exists()
    assert "extra/a3.txt" in result.files and third.exists()


@pytest.mark.parametrize("what", ["missing", "extra"])
def test_the_snapshot_is_checked_against_what_the_store_listing_gave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    what: str,
) -> None:
    """A staged file lost, or a stray one added, after the copy: not only the manifest is compared."""
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    _put(s, "extra/a.txt")
    real = recovery._list_files

    def listing(root: Path):
        if root.name.startswith(".tmp-"):
            if what == "missing":
                (root / "extra" / "a.txt").unlink()
            else:
                (root / "extra" / "stray.txt").write_text("x", encoding="utf-8")
        return real(root)

    monkeypatch.setattr(recovery, "_list_files", listing)

    with pytest.raises(ValueError, match="does not hold exactly the files"):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    _no_snapshot(tmp_path / "recovery")


# ---------------------------------------------------------- one budget for the passes


class _FakePasses:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, durations: list[float], clock: _Clock) -> None:
        self.starts: list[float] = []

        def clone(store_dir, staging, *, use_hardlinks):
            self.starts.append(clock.now)
            assert len(self.starts) <= 10, "the passes never stopped"
            clock.now += durations[min(len(self.starts), len(durations)) - 1]
            raise recovery._Vanished("gone")

        monkeypatch.setattr(recovery, "_clone_tree", clone)


@pytest.mark.parametrize(
    ("durations", "expected_passes"),
    [
        pytest.param([0.0], 3, id="passes-run-out-not-time"),
        pytest.param([4.0], 1, id="slow-first-pass-no-second"),
        pytest.param([2.0, 1.5], 2, id="time-runs-out-before-the-third-pass"),
        pytest.param([1.0, 4.0], 2, id="slow-later-pass"),
    ],
)
def test_the_passes_share_one_budget_and_then_the_backup_says_run_it_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durations: list[float],
    expected_passes: int,
) -> None:
    s = _store(tmp_path / "project")
    clock = _Clock()
    monkeypatch.setattr(recovery, "_monotonic", clock)
    passes = _FakePasses(monkeypatch, durations, clock)

    with pytest.raises(recovery.BackupStoreChanged, match="Run agenttalk backup again"):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert len(passes.starts) == expected_passes
    _no_snapshot(tmp_path / "recovery")
    _locks_are_free(s)


def test_time_spent_throwing_a_pass_away_counts_against_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path / "project")
    clock = _Clock()
    monkeypatch.setattr(recovery, "_monotonic", clock)
    passes = _FakePasses(monkeypatch, [0.0], clock)
    real_rmtree = shutil.rmtree

    def slow_cleanup(path, *a, **kw):
        if not kw.get("ignore_errors"):
            clock.now += 5.0
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(recovery.shutil, "rmtree", slow_cleanup)

    with pytest.raises(recovery.BackupStoreChanged):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert len(passes.starts) == 1
    _no_snapshot(tmp_path / "recovery")


def test_a_slow_pass_that_completes_is_not_refused_the_budget_only_limits_new_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    clock = _Clock()
    monkeypatch.setattr(recovery, "_monotonic", clock)
    real = recovery._clone_tree

    def slow(*a, **kw):
        clock.now += 60.0
        return real(*a, **kw)

    monkeypatch.setattr(recovery, "_clone_tree", slow)

    assert recovery.create_backup(s, dest_root=tmp_path / "recovery").file_count > 0


def test_a_failed_cleanup_of_a_thrown_away_pass_refuses_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path / "project")
    clock = _Clock()
    monkeypatch.setattr(recovery, "_monotonic", clock)
    passes = _FakePasses(monkeypatch, [0.0], clock)
    real_rmtree = shutil.rmtree

    def cleanup_denied(path, *a, **kw):
        if not kw.get("ignore_errors"):
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(recovery.shutil, "rmtree", cleanup_denied)

    with pytest.raises(PermissionError):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert len(passes.starts) == 1  # no new pass on top of a staging folder that could not be emptied
    _no_snapshot(tmp_path / "recovery")
    _locks_are_free(s)


# ----------------------------------------------------------------- listing errors


def _scandir_fault(monkeypatch: pytest.MonkeyPatch, folder: str, error: OSError, where: str, times: int | None):
    """os.scandir fails for `folder`: on opening it, part-way through reading it, or on one
    entry's details. `times` failures, then it works (None: always). Returns the failure count."""
    real, count = os.scandir, [0]  # only the backup's own listing fails; the test fence lists folders too

    class Entry:
        def __init__(self, entry) -> None:
            self._e, self.name, self.path = entry, entry.name, entry.path

        def is_dir(self, *a, **kw):
            return self._e.is_dir(*a, **kw)

        def is_file(self, *a, **kw):
            raise error

    class Scan:
        def __init__(self, cm) -> None:
            self._cm = cm

        def __enter__(self):
            it = self._cm.__enter__()
            return self._walk(it)

        def _walk(self, it):
            for i, entry in enumerate(it):
                if where == "iterate" and i == 1:
                    raise error
                yield Entry(entry) if where == "details" and i == 0 else entry

        def __exit__(self, *a):
            return self._cm.__exit__(*a)

    def scandir(path="."):
        if Path(path).name == folder and (times is None or count[0] < times):
            count[0] += 1
            if where == "open":
                raise error
            return Scan(real(path))
        return real(path)

    class BackupOs:
        def __getattr__(self, name):
            return getattr(os, name)

    BackupOs.scandir = staticmethod(scandir)
    monkeypatch.setattr(recovery, "os", BackupOs())
    return count


def _two_files(s: Store) -> None:
    _put(s, "extra/a.txt", "one")
    _put(s, "extra/b.txt", "two")


@pytest.mark.parametrize("where", ["open", "iterate", "details"])
def test_a_listing_error_because_it_vanished_starts_a_fresh_complete_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    _two_files(s)
    failures = _scandir_fault(monkeypatch, "extra", FileNotFoundError(errno.ENOENT, "gone"), where, times=1)

    result = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert failures[0] == 1
    assert {"extra/a.txt", "extra/b.txt"} <= set(result.files)  # complete, not the shorter list


@pytest.mark.parametrize("where", ["open", "iterate", "details"])
def test_a_listing_error_that_does_not_go_away_refuses_instead_of_a_partial_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    _two_files(s)
    _scandir_fault(monkeypatch, "extra", PermissionError(errno.EACCES, "Access is denied"), where, times=None)

    with pytest.raises(PermissionError):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    _no_snapshot(tmp_path / "recovery")
    _locks_are_free(s)


def test_a_folder_that_keeps_vanishing_runs_out_of_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    _two_files(s)
    failures = _scandir_fault(monkeypatch, "extra", FileNotFoundError(errno.ENOENT, "gone"), "open", times=None)

    with pytest.raises(recovery.BackupStoreChanged):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert failures[0] == 3
    _no_snapshot(tmp_path / "recovery")


def test_a_denied_copy_refuses_it_is_neither_skipped_nor_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows a file that is being deleted can answer "access denied"; skipping it could drop a record."""
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    victim = _put(s, "extra/a.txt")
    real_copy, attempts = shutil.copy2, []

    def copy2(src, dst, *a, **kw):
        if Path(src) == victim:
            attempts.append(1)
            raise PermissionError(errno.EACCES, "Access is denied", str(src))
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(recovery.shutil, "copy2", copy2)

    with pytest.raises(PermissionError):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert attempts == [1]
    _no_snapshot(tmp_path / "recovery")


def test_a_copy_that_fails_with_the_source_still_there_is_an_error_not_a_vanished_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows answers "file not found" for a destination path that is too long. That is not the store changing:
    three fresh passes would not help and "the store kept changing" would be untrue."""
    s = _store(tmp_path / "project")
    _copies(monkeypatch)
    victim = _put(s, "extra/a.txt")
    real_copy, attempts = shutil.copy2, []

    def copy2(src, dst, *a, **kw):
        if Path(src) == victim:
            attempts.append(1)
            raise FileNotFoundError(errno.ENOENT, "The system cannot find the path specified", str(dst))
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(recovery.shutil, "copy2", copy2)

    with pytest.raises(FileNotFoundError):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert attempts == [1]
    _no_snapshot(tmp_path / "recovery")


# ------------------------------------------------------ through the real command line


@pytest.fixture()
def _cli_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    s = _store(tmp_path / "project")
    monkeypatch.chdir(s.root)
    monkeypatch.setenv("AGENTTALK_RECOVERY_DIR", str(tmp_path / "recovery"))
    return s


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (FileNotFoundError(errno.ENOENT, "gone"), "Run agenttalk backup again"),
        (PermissionError(errno.EACCES, "Access is denied"), "Access is denied"),
    ],
)
def test_agenttalk_backup_that_cannot_finish_exits_2_with_a_message_and_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _cli_store: Store,
    error: OSError,
    words: str,
) -> None:
    s = _cli_store
    _copies(monkeypatch)
    _two_files(s)
    _scandir_fault(monkeypatch, "extra", error, "open", times=None)

    assert cli.console_main(["backup"]) == 2

    out, err = capsys.readouterr()
    assert err.startswith("agenttalk backup: ") and words in err
    assert "Traceback" not in err + out
    _no_snapshot(tmp_path / "recovery")
    _locks_are_free(s)


def test_agenttalk_backup_works_again_once_the_store_stops_changing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _cli_store: Store,
) -> None:
    _copies(monkeypatch)
    _two_files(_cli_store)
    _scandir_fault(monkeypatch, "extra", FileNotFoundError(errno.ENOENT, "gone"), "open", times=3)

    assert cli.console_main(["backup"]) == 2
    capsys.readouterr()
    assert cli.console_main(["backup"]) == 0
    assert len(list((tmp_path / "recovery").rglob("manifest.json"))) == 1


# ----------------------------------------------- real folders that must be copied, not linked


def _plain_dir(tmp_path: Path, rels: list[str]) -> tuple[Path, Path]:
    src, staging = tmp_path / "src", tmp_path / "staging"
    staging.mkdir()
    for rel in rels:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x", encoding="utf-8")
    return src, staging


@pytest.mark.parametrize("rel", COPY_ONLY_FILES)
def test_a_file_that_a_writer_checks_for_one_name_is_copied_never_linked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rel: str,
) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, [rel, "state/other.json"])

    clone = recovery._clone_tree(src, staging, use_hardlinks=True)

    assert (clone.linked, clone.copied, clone.on_purpose) == (1, 0, 1)
    assert os.stat(src / rel).st_nlink == 1
    assert os.stat(src / "state/other.json").st_nlink == 2


def test_the_launch_request_archive_is_ordinary_data_and_is_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, ["state/launch-requests/archive/lr-1.json", "state/launch-requests/a/b/c.json"])

    clone = recovery._clone_tree(src, staging, use_hardlinks=True)

    assert (clone.linked, clone.on_purpose) == (2, 0)


@pytest.mark.parametrize("only", [[PREPARE], [UNLINK], COPY_ONLY_FILES[:1], COPY_ONLY_FILES[1:2], COPY_ONLY_FILES[2:]])
def test_the_link_probe_never_tries_a_helper_or_a_copy_only_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    only: list[str],
) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, [f"extra/{only[0]}" if only[0].startswith(".") else only[0]])
    tried: list[str] = []
    real_link = os.link
    monkeypatch.setattr(
        recovery.os, "link", lambda s, d, *a, **kw: (tried.append(str(s)), real_link(s, d, *a, **kw))[1]
    )

    assert recovery._probe_hardlink_support(src, staging) is False  # nothing eligible to try

    assert tried == []


CASE_VARIANTS = [
    "STATE/launch-requests/lr-1.json",
    "State/Launch-Requests/lr-1.json",
    "LANE-DELIVERIES/a.json",
    "Lane-Deliveries/.PREPARED/x.json",
]


@pytest.mark.parametrize("rel", CASE_VARIANTS)
def test_the_copy_only_folders_match_whatever_the_case_the_disk_spells_them_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rel: str,
) -> None:
    """On Windows (and a case-insensitive Mac volume) these are the very files the writers make."""
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, [rel, "other/x.json"])

    clone = recovery._clone_tree(src, staging, use_hardlinks=True)

    assert (clone.linked, clone.copied, clone.on_purpose) == (1, 0, 1)
    assert os.stat(src / rel).st_nlink == 1


@pytest.mark.parametrize("rel", CASE_VARIANTS)
def test_the_link_probe_skips_the_copy_only_folders_in_any_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rel: str,
) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, [rel])
    tried: list[str] = []
    real_link = os.link
    monkeypatch.setattr(
        recovery.os, "link", lambda s, d, *a, **kw: (tried.append(str(s)), real_link(s, d, *a, **kw))[1]
    )

    assert recovery._probe_hardlink_support(src, staging) is False

    assert tried == []


def test_the_launch_request_archive_is_ordinary_in_any_case_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, ["STATE/Launch-Requests/ARCHIVE/lr-1.json"])

    assert recovery._clone_tree(src, staging, use_hardlinks=True).linked == 1


def test_the_link_probe_still_links_an_ordinary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, [PREPARE, COPY_ONLY_FILES[1], "state/other.json"])

    assert recovery._probe_hardlink_support(src, staging) is True


# ---------------------------------- real writers working while a backup runs inside their window


@pytest.fixture()
def _writer_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Back the store up in the moment a real writer has linked its file into place but not yet
    removed its private name: the window where an extra name on the file fails the writer."""
    _links(monkeypatch, tmp_path)
    taken: list = []

    def arm(s: Store, folder_name: str, final_glob: str) -> list:
        real = store_mod._unlink_if_same_file

        def unlink(path, identity):
            # a private name in this folder, while the real file is already in place (a lock also makes some)
            if (
                path.name.endswith(".prepare")
                and path.parent.name == folder_name
                and list(path.parent.glob(final_glob))
            ):
                taken.append(recovery.create_backup(s, dest_root=tmp_path / f"recovery-{len(taken)}"))
            return real(path, identity)

        monkeypatch.setattr(store_mod, "_unlink_if_same_file", unlink)
        return taken

    return arm


def test_a_launch_request_is_created_while_a_backup_runs(tmp_path: Path, _writer_window) -> None:
    s = _store(tmp_path / "project")
    taken = _writer_window(s, "launch-requests", "lr-1.json")

    s.write_launch_request({"request_id": "lr-1", "note": "n"})

    final = s.launch_requests_dir / "lr-1.json"
    assert json.loads(final.read_text(encoding="utf-8"))["note"] == "n"
    assert os.stat(final).st_nlink == 1 and taken
    for i, r in enumerate(taken):
        assert not os.path.samefile(r.destination / "state/launch-requests/lr-1.json", final)
        assert not [f for f in r.files if f.endswith(".prepare")]
        restored = _restore_and_take_locks(r, tmp_path / f"restored-{i}")
        assert restored.read_launch_request("lr-1")["note"] == "n"


@pytest.mark.parametrize("folder", [".prepared", "lane-deliveries"])
def test_both_lane_delivery_writers_succeed_while_a_backup_runs(
    tmp_path: Path,
    _writer_window,
    folder: str,
) -> None:
    from test_lanes import _advisory_delivery_fixture, _repo, _run

    root, base = _repo(tmp_path)
    s = Store(root)
    taken = _writer_window(s, folder, "w-*.json")
    head = _advisory_delivery_fixture(root, base, "w")

    assert _run(["lane", "deliver", "--id", "w", "--from", "dev", "--head", head], root) == 0

    assert taken, "the writer never reached its window"
    delivered = sorted((s.dir / "lane-deliveries").rglob("w-*.json"))
    assert delivered
    for path in delivered:
        assert os.stat(path).st_nlink == 1
    for i, r in enumerate(taken):
        assert not [f for f in r.files if f.endswith(".prepare")]
        snap = [f for f in r.files if f.startswith("lane-deliveries/")]
        assert snap
        for f in snap:
            if (s.dir / f).exists():
                assert not os.path.samefile(r.destination / f, s.dir / f)
        restored = _restore_and_take_locks(r, tmp_path / f"restored-{i}")
        for f in snap:
            if f.endswith(".json"):
                json.loads((restored.dir / f).read_text(encoding="utf-8"))


# ---------------------------------------- one more writer later must not be forgotten


def test_only_the_known_writers_use_the_no_replace_publisher() -> None:
    """Every caller of `_publish_text_no_replace` that is not the lock itself writes a data file
    that a backup must copy. A new caller fails this test: add its folder to
    store.is_copy_only_backup_path, add a window test above for it, then list it here."""
    src = Path(store_mod.__file__).parent
    callers: set[tuple[str, str]] = set()
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    if name == "_publish_text_no_replace":
                        callers.add((path.name, fn.name))
    assert callers == {
        ("store.py", "_exclusive_lock_unordered"),  # the lock itself: never backed up
        ("store.py", "write_launch_request"),  # state/launch-requests/<name>
        ("lanes.py", "write_prepared_delivery_artifact"),  # lane-deliveries/.prepared/**
        ("lanes.py", "publish_delivery_artifact"),  # lane-deliveries/**
    }


# ----------------------------------------------- what "hardlink" says, for every mix


def _result(**kw) -> recovery.BackupResult:
    base = {
        "project_id": "p",
        "destination": Path("d"),
        "manifest_path": Path("m"),
        "manifest_hash": "h",
        "file_count": 0,
        "total_bytes": 0,
        "hardlink_used": False,
        "sequence_at_snapshot": None,
        "fence_seconds": 0.0,
    }
    return recovery.BackupResult(**{**base, **kw})


@pytest.mark.parametrize(
    ("kw", "words"),
    [
        ({"hardlink_used": True, "files_linked": 5}, "hardlink"),
        (
            {"files_linked": 5, "files_copied_on_purpose": 2},
            "hardlink (2 launch-request and lane-delivery files are always copied)",
        ),
        ({"files_linked": 3, "files_link_failed": 2}, "hardlink (2 copied because a link failed)"),
        ({"files_link_failed": 4}, "copy (every link failed: 4 copied because a link failed)"),
        (
            {"files_link_failed": 4, "files_copied_on_purpose": 2},
            "copy (every link failed: 4 copied because a link failed, "
            "2 launch-request and lane-delivery files are always copied)",
        ),
        ({"files_copied": 4}, "copy (no hardlink support)"),
        (
            {"files_copied": 4, "files_copied_on_purpose": 1},
            "copy (no hardlink support); 1 launch-request and lane-delivery files are always copied",
        ),
        (
            {"files_copied_on_purpose": 3},
            "copy (no file was eligible for linking; 3 launch-request and lane-delivery files are always copied)",
        ),
        ({}, "copy (no file was eligible for linking)"),
    ],
)
def test_the_mode_line_is_true_for_every_mix(kw: dict, words: str) -> None:
    assert recovery.mode_summary(_result(**kw)) == words


def test_only_ordinary_files_all_linked_says_hardlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _links(monkeypatch, tmp_path)
    s = _store(tmp_path / "project")

    r = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert r.hardlink_used is True and r.files_linked == r.file_count and r.files_copied_on_purpose == 0
    assert json.loads(r.manifest_path.read_text())["hardlink_used"] is True
    assert recovery.mode_summary(r) == "hardlink"


def test_mixed_files_say_not_every_file_was_linked_and_name_the_copied_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _links(monkeypatch, tmp_path)
    s = _store(tmp_path / "project")
    s.write_launch_request({"request_id": "lr-1"})
    _put(s, "lane-deliveries/l1-abc.json")
    monkeypatch.setenv("AGENTTALK_RECOVERY_DIR", str(tmp_path / "r"))

    assert cli.main(["--root", str(s.root), "backup"]) == 0

    out = capsys.readouterr().out
    assert "mode:       hardlink (2 launch-request and lane-delivery files are always copied)" in out
    assert "no hardlink support" not in out
    manifest = json.loads(next((tmp_path / "r").rglob("manifest.json")).read_text())
    assert manifest["hardlink_used"] is False  # "every file was linked" stays the meaning


def test_json_output_keeps_exactly_its_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _store(tmp_path / "project")
    monkeypatch.setenv("AGENTTALK_RECOVERY_DIR", str(tmp_path / "recovery"))

    assert cli.main(["--root", str(s.root), "backup", "--json"]) == 0

    assert set(json.loads(capsys.readouterr().out)) == {
        "project_id",
        "destination",
        "manifest_path",
        "manifest_hash",
        "file_count",
        "total_bytes",
        "hardlink_used",
        "sequence_at_snapshot",
        "fence_seconds",
    }


def test_a_store_with_only_copy_only_files_copies_them_on_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, COPY_ONLY_FILES)

    clone = recovery._clone_tree(src, staging, use_hardlinks=recovery._probe_hardlink_support(src, staging))

    assert (clone.linked, clone.copied, clone.on_purpose) == (0, 0, 3)


def test_a_store_with_no_eligible_file_copies_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _links(monkeypatch, tmp_path)
    src, staging = _plain_dir(tmp_path, [f"x/{PREPARE}", "x/a.lock"])

    clone = recovery._clone_tree(src, staging, use_hardlinks=recovery._probe_hardlink_support(src, staging))

    assert (clone.linked, clone.copied, clone.on_purpose, clone.included) == (0, 0, 0, [])


def test_a_failed_link_is_copied_and_the_mode_line_says_a_link_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _links(monkeypatch, tmp_path)
    s = _store(tmp_path / "project")
    real_link = os.link

    def link(src, dst, *a, **kw):
        if str(src).endswith(".json") and str(dst).startswith(str(tmp_path / "recovery")):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(recovery.os, "link", link)
    monkeypatch.setattr(recovery, "_probe_hardlink_support", lambda *_a, **_kw: True)

    r = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert r.files_link_failed > 0 and r.files_linked > 0 and r.files_copied == 0 and r.hardlink_used is False
    assert f"hardlink ({r.files_link_failed} copied because a link failed)" == recovery.mode_summary(r)
    assert json.loads(r.manifest_path.read_text())["hardlink_used"] is False


def test_when_every_link_fails_after_a_good_probe_the_mode_line_does_not_say_no_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _links(monkeypatch, tmp_path)
    s = _store(tmp_path / "project")
    s.write_launch_request({"request_id": "lr-1"})
    real_link = os.link

    def link(src, dst, *a, **kw):
        if str(dst).startswith(str(tmp_path / "r")):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(recovery.os, "link", link)
    monkeypatch.setattr(recovery, "_probe_hardlink_support", lambda *_a, **_kw: True)
    monkeypatch.setenv("AGENTTALK_RECOVERY_DIR", str(tmp_path / "r"))

    assert cli.main(["--root", str(s.root), "backup"]) == 0

    out = capsys.readouterr().out
    assert "mode:       copy (every link failed: " in out and "1 launch-request and lane-delivery" in out
    assert "no hardlink support" not in out
    manifest = json.loads(next((tmp_path / "r").rglob("manifest.json")).read_text())
    assert manifest["hardlink_used"] is False


def test_a_store_that_cannot_link_says_no_hardlink_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store(tmp_path / "project")
    _copies(monkeypatch)

    r = recovery.create_backup(s, dest_root=tmp_path / "recovery")

    assert (r.files_linked, r.files_copied, r.hardlink_used) == (0, r.file_count, False)
    assert recovery.mode_summary(r) == "copy (no hardlink support)"
