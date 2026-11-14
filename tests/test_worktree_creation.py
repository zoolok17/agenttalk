"""Creation-only path policy; shared launch-use resolution must remain strict."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import cli, lanes


@pytest.fixture
def creation_store(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / ".git").mkdir()
    marker = root / lanes.WORKTREE_MARKER_FILENAME
    marker.write_bytes(b"existing marker\r\n")
    return SimpleNamespace(root=root)


def test_788_relative_root_is_store_based_before_path_use(creation_store, monkeypatch, tmp_path):
    """[regression T1/T3] The process folder is not the creation base."""
    monkeypatch.chdir(tmp_path)
    root = lanes.worktrees_root(creation_store, ".lanes/a/b")
    assert root == creation_store.root / ".lanes/a/b" and root.is_absolute()
    assert not root.exists()


@pytest.mark.parametrize("value", ["sub/../lanes", "missing/../existing", "../outside", "sub/../../x"])
@pytest.mark.parametrize("absolute", [False, True])
def test_788_parent_component_is_rejected_before_creation(creation_store, value, absolute):
    """[new refusal T2/T7] Raw components, not a normalized substring."""
    root = creation_store.root
    raw = str(root) + "/" + value if absolute else value
    before = list(root.rglob("*"))
    marker = root / lanes.WORKTREE_MARKER_FILENAME
    original = marker.read_bytes()
    with pytest.raises(lanes.LaneError, match=r'contains a "\.\." path component'):
        cli._prepare_worktrees_root(creation_store, raw)
    assert list(root.rglob("*")) == before and marker.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows raw spelling policy")
@pytest.mark.parametrize("part", [". .", "...", " ", ".. ", " ."])
@pytest.mark.parametrize("absolute", [False, True])
def test_788_windows_dot_space_components_have_their_own_refusal(creation_store, part, absolute):
    """[new refusal T7] Empty separator components and single dot are different."""
    value = "sub/" + part + "/new"
    if absolute:
        value = str(creation_store.root) + "/" + value
    with pytest.raises(lanes.LaneError, match="dots and spaces"):
        cli._prepare_worktrees_root(creation_store, value)
    assert not (creation_store.root / "sub").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows raw spelling policy")
@pytest.mark.parametrize(
    "shape", ["own-drive", "other-drive", "backslash-root", "slash-root", "extended", "device", "stream"]
)
def test_788_windows_special_roots_are_refused_on_the_raw_value(creation_store, shape):
    """[new refusal T11] Includes the actual store drive, not just another drive."""
    root = creation_store.root
    drive = root.drive
    values = {
        "own-drive": drive + "lanes",
        "other-drive": ("D:" if drive.upper() != "D:" else "C:") + "lanes",
        "backslash-root": "\\lanes",
        "slash-root": "/lanes",
        "extended": "\\\\?\\" + str(root / "lanes"),
        "device": "\\\\.\\" + str(root / "lanes"),
        "stream": "lanes:stream",
    }
    message = (
        "depends on a drive"
        if shape in {"own-drive", "other-drive", "backslash-root", "slash-root"}
        else "device, extended or stream"
    )
    with pytest.raises(lanes.LaneError, match=message):
        cli._prepare_worktrees_root(creation_store, values[shape])
    assert not (root / "lanes").exists()


@pytest.mark.parametrize("value", ["name..part", "./lanes", "a//./b/", "~"])
def test_788_ordinary_relative_names_save_new_absolute_values(creation_store, value):
    """[changed T5/T7/T13] Success is old behavior; absolute values are new."""
    assert lanes.worktrees_root(creation_store, value) == creation_store.root / value
    result = cli._prepare_worktrees_root(creation_store, value)
    assert result.is_absolute() and result.is_dir()
    assert (result / lanes.WORKTREE_MARKER_FILENAME).is_file()


@pytest.mark.parametrize("configured", [None, "absolute"])
@pytest.mark.parametrize("git_file", [False, True])
def test_788_default_absolute_and_git_file_boundary_controls(creation_store, configured, git_file):
    """[control T6 + binding .git-file control] A boundary need not be a directory."""
    root = creation_store.root
    if git_file:
        (root / ".git").rmdir()
        (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    expected = root / ("absolute" if configured else ".worktrees")
    assert cli._prepare_worktrees_root(creation_store, str(expected) if configured else None) == expected


@pytest.mark.parametrize("case", ["outside", "sibling", "store", "git", "git-child"])
def test_788_boundary_refusals_preserve_existing_marker_bytes(creation_store, case):
    """[changed T9] Refusal already existed; precise new diagnostics differ."""
    root = creation_store.root
    target = {
        "outside": root.parent / "outside",
        "sibling": root.parent / (root.name + "-other"),
        "store": root,
        "git": root / ".git",
        "git-child": root / ".git/x",
    }[case]
    marker = root / lanes.WORKTREE_MARKER_FILENAME
    original = marker.read_bytes()
    existed = target.exists()
    message = "outside the store folder" if case in {"outside", "sibling"} else "store folder itself or inside its .git"
    with pytest.raises(lanes.LaneError, match=message) as exc:
        cli._prepare_worktrees_root(creation_store, str(target))
    assert "nothing was created" in str(exc.value)
    assert marker.read_bytes() == original and target.exists() == existed


def test_788_missing_suffix_uses_creation_policy_not_idle_policy(creation_store):
    """[regression T3; control T15] Multiple missing levels are creation-only."""
    target = creation_store.root / "deep/a/b"
    assert lanes.resolve_creation_folder(target) == lanes.canonical_host_path(target)
    with pytest.raises(OSError):
        lanes.canonical_host_path_checked(target)
    assert not target.exists()


def test_788_file_parent_cannot_be_a_creation_directory(creation_store):
    """[changed T10] File ancestors are not walked upward as missing entries."""
    file = creation_store.root / "file"
    file.write_bytes(b"keep")
    with pytest.raises(lanes.LaneError, match="could not be established"):
        cli._prepare_worktrees_root(creation_store, str(file / "new"))
    assert file.read_bytes() == b"keep"


@pytest.mark.parametrize("stage", ["lstat", "strict-realpath"])
@pytest.mark.parametrize("api", ["prepare", "helper"])
def test_788_permission_error_has_the_creation_diagnostic(creation_store, monkeypatch, stage, api):
    """[changed T14] Explicit OSError mapping, not the top-level exit handler."""
    anchor = creation_store.root / "anchor"
    anchor.mkdir()
    target = anchor / "new/leaf"
    real_lstat, realpath = lanes.os.lstat, lanes.os.path.realpath

    def lstat(path, *args, **kwargs):
        if Path(path) == anchor:
            raise PermissionError("injected lookup denied")
        return real_lstat(path, *args, **kwargs)

    def resolve(path, *, strict=False):
        if strict and Path(path) == anchor:
            raise PermissionError("injected strict lookup denied")
        return realpath(path, strict=strict)

    with monkeypatch.context() as fault:
        if stage == "lstat":
            fault.setattr(lanes.os, "lstat", lstat)
        else:
            fault.setattr(lanes.os.path, "realpath", resolve)
        with pytest.raises(lanes.LaneError, match="could not be established") as exc:
            if api == "helper":
                lanes.resolve_creation_folder(target)
            else:
                cli._prepare_worktrees_root(creation_store, str(target))
    assert "injected" in str(exc.value) and "nothing was created" in str(exc.value)
    assert str(exc.value).count("could not be established") == 1
    assert not (anchor / "new").exists()


@pytest.mark.parametrize("api", ["prepare", "helper"])
def test_788_nul_value_error_has_the_creation_diagnostic(creation_store, api):
    """[changed T14] ValueError mapping is distinct from PermissionError."""
    with pytest.raises(lanes.LaneError, match="could not be established") as exc:
        if api == "helper":
            lanes.resolve_creation_folder(creation_store.root / "bad\0name")
        else:
            cli._prepare_worktrees_root(creation_store, "bad\0name")
    assert "nothing was created" in str(exc.value)
    assert str(exc.value).count("could not be established") == 1
    assert not (creation_store.root / "bad").exists()


def test_788_missing_root_walk_terminates(creation_store, monkeypatch):
    """[new refusal binding] No real unavailable drive or network share is needed."""
    target = creation_store.root / "missing/a/b"
    calls = []
    real = lanes.os.lstat
    ancestors = {target, *target.parents}

    def missing(path, *args, **kwargs):
        if Path(path) in ancestors:
            calls.append(Path(path))
            if len(calls) > len(ancestors) + 1:
                raise AssertionError("creation walk did not stop at its root")
            raise FileNotFoundError("injected absent volume")
        return real(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(lanes.os, "lstat", missing)
        with pytest.raises(lanes.LaneError, match="could not be established"):
            lanes.resolve_creation_folder(target)
    assert len(calls) <= len(ancestors)


@pytest.mark.parametrize("stage", ["mkdir", "marker"])
def test_788_preparation_write_errors_do_not_claim_nothing_created(creation_store, monkeypatch, stage):
    """[changed binding] Preparation may have created the directory before failure."""
    target = creation_store.root / "new-root"
    real_mkdir, real_write = Path.mkdir, Path.write_text

    def mkdir(path, *args, **kwargs):
        if path == target:
            raise PermissionError("injected mkdir denied")
        return real_mkdir(path, *args, **kwargs)

    def write(path, *args, **kwargs):
        if path == target / lanes.WORKTREE_MARKER_FILENAME:
            raise PermissionError("injected marker denied")
        return real_write(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "mkdir" if stage == "mkdir" else "write_text", mkdir if stage == "mkdir" else write)
        with pytest.raises(lanes.LaneError, match="could not prepare the worktrees root") as exc:
            cli._prepare_worktrees_root(creation_store, str(target))
    assert "nothing was created" not in str(exc.value)
    assert target.exists() == (stage == "marker")
    assert not (target / lanes.WORKTREE_MARKER_FILENAME).exists()


@pytest.mark.parametrize(
    "error",
    [PermissionError("selection denied"), ValueError("selection invalid"), lanes.LaneError("dedicated raw refusal")],
)
def test_788_root_selection_errors_are_mapped_without_rewrapping_lane_errors(creation_store, monkeypatch, error):
    """[changed T14] LaneError is itself a ValueError; preserve its dedicated message."""

    def select(*args):
        raise error

    monkeypatch.setattr(lanes, "worktrees_root", select)
    with pytest.raises(lanes.LaneError) as exc:
        cli._prepare_worktrees_root(creation_store, "lanes")
    if isinstance(error, lanes.LaneError):
        assert exc.value is error and str(exc.value) == "dedicated raw refusal"
    else:
        assert str(exc.value).count("could not be established") == 1
        assert str(error) in str(exc.value)


def test_788_nonempty_unmanaged_root_keeps_existing_refusal(creation_store):
    """[control] Adding error mapping must not rewrap this existing refusal."""
    target = creation_store.root / "unmanaged"
    target.mkdir()
    (target / "existing.txt").write_bytes(b"keep")
    with pytest.raises(lanes.LaneError) as exc:
        cli._prepare_worktrees_root(creation_store, str(target))
    assert str(exc.value) == "worktrees root exists and is non-empty but lacks the agenttalk marker"
    assert (target / "existing.txt").read_bytes() == b"keep"
    assert not (target / lanes.WORKTREE_MARKER_FILENAME).exists()
