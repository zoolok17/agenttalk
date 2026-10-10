"""Release one merged checkout, retaining its branch and commits.

Like janitor, this requires a quiet filesystem during removal. No force,
preservation commit, recursive-delete fallback or branch deletion is used.
"""
from __future__ import annotations

import dataclasses
import ctypes
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
from pathlib import Path

from . import janitor
from . import store as store_mod


class Refused(ValueError):
    """A check could not establish that this checkout is disposable."""


def _git(repo: Path, *args: str) -> tuple[int, str]:
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        if os.environ.get(name):
            raise Refused(f"{name} overrides Git's repository; clear it before release")
    executable = shutil.which("git")
    if executable is None:
        raise Refused("git is not available")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    try:
        result = subprocess.run(  # nosec B603
            [executable, "-C", str(repo), "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="strict", env=env, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise Refused(f"git could not complete the check ({type(exc).__name__})") from exc
    return result.returncode, result.stdout


def _read_git(repo: Path, *args: str) -> str:
    rc, text = _git(repo, *args)
    if rc:
        raise Refused(f"git {args[0]} failed (exit {rc}); checkout kept")
    return text.strip()


def _identities(cfg: janitor.JanitorConfig, target: Path | None = None) -> dict:
    ids, errors = janitor._ancestor_ids(cfg)
    if errors:
        raise Refused(f"{errors[0][0]}: {errors[0][1]}")
    if target is not None:
        extra, errors = janitor._ancestor_ids(dataclasses.replace(cfg, scratch_root=target))
        if errors:
            raise Refused(f"{errors[0][0]}: {errors[0][1]}")
        ids.update(extra)
    return ids


def _unchanged(ids: dict) -> None:
    reason = janitor._ancestor_change_reason(ids)
    if reason:
        raise Refused(reason)


def _registered(repo: Path) -> dict[Path, set[str]]:
    # NUL framing also handles paths containing newlines or Git quoting characters.
    out = _read_git(repo, "worktree", "list", "--porcelain", "-z")
    paths: dict[Path, set[str]] = {}
    current = None
    for field in out.split("\0"):
        if field.startswith("worktree "):
            current = Path(field[9:])
            paths[current] = set()
        elif current is not None and field:
            paths[current].add(field.split(" ", 1)[0])
    if not paths:
        raise Refused("git did not identify any registered worktrees")
    return paths


def _fresh_default(repo: Path, remote: str = "origin") -> str:
    # Ask the remote, not the possibly stale local origin/HEAD alias. Do not
    # guess main/master or trust a local merge that has not been pushed.
    # ``remote`` is origin's checked folder under the store fence (check_fence).
    out = _read_git(repo, "ls-remote", "--symref", remote, "HEAD")
    refs = [line.split("\t")[0][5:] for line in out.splitlines()
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD")]
    if len(refs) != 1:
        raise Refused("origin did not name one default branch; no release is possible")
    ref = refs[0]
    _read_git(repo, "check-ref-format", ref)
    tracking = "refs/remotes/origin/" + ref[len("refs/heads/"):]
    _read_git(repo, "fetch", "--no-tags", "--no-prune", "--no-recurse-submodules", remote, f"+{ref}:{tracking}")
    return _read_git(repo, "rev-parse", "--verify", tracking + "^{commit}")


def _local_repository(url: str, repo: Path) -> Path | None:
    """The folder Git reads for remote address ``url`` when that is a local repository, else
    None: a network or helper address, a file URL with a host or a percent escape, or a path
    starting with ~, which Git expands from a home folder."""
    if url.startswith("~"):
        return None
    if url.startswith("file://"):
        path = url[len("file://"):]
        if "%" in path or not path.startswith("/"):
            return None
        return Path(path[1:] if re.match(r"^/[A-Za-z]:[\\/]", path) else path)    # file:///C:/...
    if "::" in url or "://" in url:
        return None
    if re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith(("/", "\\")):
        return Path(url)
    if ":" in re.split(r"[\\/]", url, maxsplit=1)[0]:
        return None                                        # host:path, Git's short form for ssh
    return repo / url                                      # relative: Git runs in the repository


def _promisor_remotes(repo: Path) -> list[str]:
    """The remotes a partial clone fetches missing objects from, whenever a command needs one."""
    names = []
    rc, out = _git(repo, "config", "--type=bool", "--get-regexp", r"^remote\..*\.promisor$")
    for key, _, value in (line.partition(" ") for line in (out.splitlines() if rc == 0 else [])):
        if value == "true":
            names.append(key[len("remote."):-len(".promisor")])
    rc, out = _git(repo, "config", "--get", "extensions.partialClone")
    if rc == 0 and out.strip():
        names.append(out.strip())
    return names


def _refuse_alternates(objects: Path, owner: str, *, own: bool = False) -> None:
    """Refuse a repository that borrows objects through an alternates file. How Git reads its
    entries (spaces, quoting, relative paths, links) is not repeated here, so none is trusted;
    the file is never opened. It is located first, links on the way followed, unless it is
    this repository's ``own``, which lies wherever the repository's own metadata does. An
    absent or empty file borrows nothing."""
    listing = objects / "info" / "alternates"
    if not own:
        store_mod.check_folder_fence(listing)
    try:
        status = os.stat(listing)
    except FileNotFoundError:
        return
    except OSError:
        store_mod.refuse_unplaced(f"{listing}, which cannot be inspected")
    if not stat.S_ISREG(status.st_mode) or status.st_size:
        store_mod.refuse_unplaced(f"{owner}, which borrows objects through {listing}")


def check_fence(cfg: janitor.JanitorConfig) -> str:
    """Under AGENTTALK_STORE_FENCE, before release asks any remote anything: Git may read only
    folders inside the fence beyond this repository's own metadata. That is the repository
    origin names (its effective address, after url.<base>.insteadOf, relative to the
    repository, links followed), as Git looks it up (path, path/.git, path.git, path.git/.git).
    Refused instead of followed: a partial clone (its remotes are fetched from on demand), and
    a repository, here or origin, that borrows objects through an alternates file. A remote
    that is not a local folder, or one whose folder cannot be worked out, is refused too.

    Returns origin's folder as one absolute path with links followed. Release hands Git that
    exact string instead of the name origin, so what was checked is what Git uses, and
    origin's own settings (an upload-pack program among them) no longer apply. A url rule
    that would still rewrite that string is refused."""
    try:
        objects = cfg.repo / _read_git(cfg.repo, "rev-parse", "--git-path", "objects")
        promisors = _promisor_remotes(cfg.repo)
        url = _read_git(cfg.repo, "ls-remote", "--get-url", "origin")
    except Refused:
        store_mod.refuse_unplaced("this repository's Git settings, which Git could not give")
    for name in promisors:
        store_mod.refuse_unplaced(f"git remote {name}, which a partial clone fetches missing objects from")
    _refuse_alternates(objects, "this repository", own=True)
    path = _local_repository(url, cfg.repo) if url else None
    if path is None:
        store_mod.refuse_unplaced("git remote origin, which is not a local folder")
    for base in dict.fromkeys((path, path.resolve())):         # as configured, and as release hands it on
        candidates = (base, Path(f"{base}.git"))
        for repository in candidates:
            store_mod.check_folder_fence(repository)
            store_mod.check_folder_fence(repository / ".git")
        for repository in candidates:
            dotgit = repository / ".git"
            if os.path.lexists(dotgit) and not dotgit.is_dir():
                store_mod.refuse_unplaced(f"git remote origin: {dotgit}, which names its repository elsewhere")
            for folder in (repository / "objects", dotgit / "objects"):
                _refuse_alternates(folder, "git remote origin")
    origin = str(path.resolve())
    try:
        rewritten = _read_git(cfg.repo, "ls-remote", "--get-url", origin) != origin
    except Refused:
        rewritten = True
    if rewritten:
        store_mod.refuse_unplaced("git remote origin, whose folder Git's url settings would rewrite")
    return origin


def _tree_size(root: Path) -> int:
    """Refuse every link, even ignored cache links; never descend through one."""
    total = 0
    pending = [root]
    while pending:
        folder = pending.pop()
        _check_delete_access(folder, directory=True)
        entries = list(folder.iterdir())
        names = {entry.name.casefold() for entry in entries}
        # Bare repositories keep their only objects here, without a .git child.
        if {"head", "objects", "refs"} <= names:
            raise Refused(f"nested bare repository: {folder.relative_to(root)}")
        for entry in entries:
            st = entry.lstat()
            if janitor._is_link_stat(st):
                raise Refused(f"link inside checkout: {entry.relative_to(root)}")
            if entry.name.casefold() == ".git" and entry.parent != root:
                raise Refused(f"nested repository: {entry.relative_to(root)}")
            if stat.S_ISDIR(st.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(st.st_mode):
                _check_delete_access(entry)
                total += st.st_size
            else:
                raise Refused(f"not a plain file: {entry.relative_to(root)}")
    return total


def _check_delete_access(path: Path, *, directory: bool = False) -> None:
    """Catch existing Windows file locks before Git starts removing any files.

    This opens no delete-on-close handle and changes nothing. As with the
    ancestor checks, a new lock acquired after this check requires quiescence.
    """
    if os.name != "nt":
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # DELETE access, share all, OPEN_EXISTING; directory handles need backup semantics.
    handle = create(str(path), 0x10000, 7, None, 3, 0x02000000 if directory else 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise Refused(f"Windows refused delete access (error {ctypes.get_last_error()}): {path.name}; "
                      "close programs using the checkout and check its permissions")
    close(handle)


def _read_record(cfg: janitor.JanitorConfig, path: Path) -> dict:
    # The state file itself and its parents are checked before the reader.
    _identities(dataclasses.replace(cfg, scratch_root=path.parent))
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {}
    if janitor._is_link_stat(st) or not stat.S_ISREG(st.st_mode):
        raise Refused(f"cannot read linked or non-file activity record: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data
    except (ValueError, OSError) as exc:
        raise Refused(f"could not read activity record {path.name}") from exc


def _names_checkout(value: object, target: Path, repo: Path, *,
                    source: str = "activity record", lane: bool = False) -> bool:
    fields = {"cwd", "launch_cwd", "workspace_path", "worktree_path"}
    if lane:
        fields.add("worktree_toplevel_canonical")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in fields and isinstance(item, str):
                path = Path(item.replace("{ROOT}", str(repo)))
                paths = [path]
                if not path.is_absolute():
                    if lane and key == "worktree_path" and item.strip() and not path.drive:
                        # Legacy lane consumers resolve against their caller's
                        # cwd. Also protect repo-relative spellings; stored
                        # canonical provenance is checked independently below.
                        paths = [repo / path, Path.cwd() / path]
                    else:
                        raise Refused(f"{source}: relative activity path {key} is ambiguous; "
                                      "record an absolute path first")
                for candidate in paths:
                    physical = candidate.resolve()
                    if target.resolve() in (physical, *physical.parents):
                        return True
            elif key in fields and item is not None:
                if not isinstance(item, str):
                    raise Refused(f"{source}: could not read activity path {key}")
            elif isinstance(item, (dict, list)) and _names_checkout(
                    item, target, repo, source=source, lane=lane):
                return True
    elif isinstance(value, list):
        return any(_names_checkout(item, target, repo, source=source, lane=lane) for item in value)
    return False


def _activity(cfg: janitor.JanitorConfig, target: Path) -> None:
    store = cfg.repo / ".agenttalk"
    state = store / "state"
    # Missing coordination directories are normal in an ordinary repository.
    # Check existing parents first; _read_record needs its parent to exist.
    _identities(dataclasses.replace(cfg, scratch_root=store))
    if not store.exists():
        return
    # The supervisor owns these files directly under the store, even when no
    # state/ directory exists. Retained launches can restart at the next tick.
    for path in (store / "supervisor-state.json", store / "supervisor.json"):
        if _names_checkout(_read_record(cfg, path), target, cfg.repo, source=path.name):
            raise Refused(f"a launch record names this checkout ({path.name}); retire it first")
    _identities(dataclasses.replace(cfg, scratch_root=state))
    if not state.exists():
        return
    lanes = _read_record(cfg, state / "lanes.json").get("lanes", {})
    if not isinstance(lanes, dict) or any(not isinstance(row, dict) for row in lanes.values()):
        raise Refused("could not read lane records")
    for lane_id, row in lanes.items():
        if row.get("status") not in {"delivered", "abandoned"} or row.get("publish_pending"):
            source = f"lanes.json lane {lane_id}"
            if _names_checkout(row, target, cfg.repo, source=source, lane=True):
                raise Refused(f"an active lane names this checkout ({source})")
    # Retained launch records are conservative blockers even if their process
    # appears stopped: a configured supervisor may restart it at the next tick.
    if _names_checkout(_read_record(cfg, state / "supervisor-state.json"), target, cfg.repo,
                       source="state/supervisor-state.json"):
        raise Refused("a launch record names this checkout (state/supervisor-state.json); retire it first")
    requests = state / "launch-requests"
    _identities(dataclasses.replace(cfg, scratch_root=requests))
    if requests.exists():
        # glob can suppress a denied directory listing; unreadable is not empty.
        for path in requests.iterdir():
            if path.suffix != ".json":
                continue
            row = _read_record(cfg, path)
            if row.get("state") not in {"archived", "failed"}:
                scope = row.get("scope") or {}
                if not isinstance(scope, dict):
                    raise Refused("could not read launch request scope")
                lane_id = row.get("lane_id") or scope.get("lane_id")
                if lane_id is not None and not isinstance(lane_id, str):
                    raise Refused("could not read launch request lane")
                if _names_checkout(row, target, cfg.repo, source=f"launch-requests/{path.name}"):
                    raise Refused(f"a launch request names this checkout ({path.name})")
                source = f"lanes.json lane {lane_id}"
                if lane_id in lanes and _names_checkout(
                        lanes[lane_id], target, cfg.repo, source=source, lane=True):
                    raise Refused(f"a launch request names this checkout ({path.name}, {source})")


def _check_tracked_content(target: Path) -> None:
    # Status can trust cached size/time even after an edit. Hash every tracked
    # file independently, using Git's clean conversion (including CRLF rules).
    for entry in _read_git(target, "ls-files", "--stage", "-z").split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, expected, stage = metadata.split()
        if stage != "0" or mode not in {"100644", "100755"}:
            raise Refused(f"tracked entry is not a plain file: {name}")
        actual = _read_git(target, "hash-object", f"--path={name}", "--", name)
        if actual != expected:
            raise Refused(f"tracked file content differs from the index: {name}")


def _check_worktree(cfg: janitor.JanitorConfig, target: Path, default: str) -> tuple[int, str]:
    records = _registered(cfg.repo)
    paths = list(records)
    matching = next((p for p in paths if janitor._same_worktree(target, p)), None)
    if matching is None:
        raise Refused("path is not a worktree registered to this repository")
    if janitor._same_worktree(target, paths[0]) or janitor._same_worktree(target, cfg.repo):
        raise Refused("the main/current repository is never released")
    if records[matching] & {"locked", "prunable", "bare"}:
        raise Refused("worktree registration is locked, prunable or bare; inspect it before release")
    physical = target.resolve()
    if not any(root.resolve() in physical.parents for root in (cfg.repo, cfg.scratch_root, cfg.tmp_root)):
        raise Refused("worktree is outside the scanned folders")
    # Check the pointer without following it before asking Git about this path.
    pointer = (target / ".git").lstat()
    if janitor._is_link_stat(pointer) or not stat.S_ISREG(pointer.st_mode):
        raise Refused("registered checkout has no plain .git pointer file")
    if not janitor._same_worktree(Path(_read_git(target, "rev-parse", "--show-toplevel")), target):
        raise Refused("Git reports a different checkout root")
    head = _read_git(target, "rev-parse", "--verify", "HEAD^{commit}")
    rc, _ = _git(cfg.repo, "merge-base", "--is-ancestor", head, default)
    if rc != 0:
        raise Refused("HEAD is not a confirmed ancestor of origin's fetched default branch; "
                      "squash merges are not accepted, even when their files match")
    _activity(cfg, target)
    # Both flags can hide edits from status AND git worktree remove. Refuse the
    # flags even on apparently clean files; never clear them on the user's behalf.
    entries = _read_git(target, "ls-files", "-v", "-z").split("\0")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
        raise Refused("index has assume-unchanged or skip-worktree entries; clear the flags and inspect the files")
    status = _read_git(target, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise Refused("worktree has uncommitted changes or untracked files")
    keep = janitor.ignored_worktree_keep_reason(target)
    if keep:
        raise Refused(keep)
    # Only eligible candidates pay for a full walk and Windows sharing checks.
    size = _tree_size(target)
    _check_tracked_content(target)
    return size, head


def release(cfg: janitor.JanitorConfig, target: Path | None, origin: str = "origin") -> tuple[int, str]:
    """None reports all registered checkouts; a path requests exactly one removal. ``origin`` is
    what Git asks for the default branch: the remote's name, or under the store fence the
    folder check_fence checked."""
    lines = []
    try:
        ids = _identities(cfg)
        if not janitor._same_worktree(cfg.repo, next(iter(_registered(cfg.repo)))):
            raise Refused("run release from the main repository so its lane and launch records are checked")
        default = _fresh_default(cfg.repo, origin)
        _unchanged(ids)
        paths = [Path(target).absolute()] if target is not None else list(_registered(cfg.repo))
        for path in paths:
            try:
                _unchanged(ids)
                candidate_ids = _identities(cfg, path)
                _unchanged(ids)
                size, head = _check_worktree(cfg, path, default)
                if target is None:
                    _unchanged(candidate_ids)
                    lines.append(f"WOULD RELEASE {path}: {size} bytes")
                    continue
                _unchanged(candidate_ids)
                # Re-read mutable eligibility, then recheck identities at the
                # deletion boundary. No stale report can authorize a removal.
                _, current_head = _check_worktree(cfg, path, default)
                if current_head != head:
                    raise Refused("checkout HEAD changed during the checks")
                _unchanged(candidate_ids)
                _unchanged(ids)
                _read_git(cfg.repo, "worktree", "remove", "--", str(path))
                lines.append(f"REMOVED {path}: branch and commits kept")
            except (Refused, OSError, ValueError, TypeError) as exc:
                lines.append(f"KEPT {path}: {exc}")
                if target is not None:
                    return 1, "\n".join(lines)
        return 0, "\n".join(lines)
    except (Refused, OSError, ValueError, TypeError) as exc:
        return 1, f"FAILED: {exc}; nothing released"
