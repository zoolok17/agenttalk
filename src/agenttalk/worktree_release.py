"""Release one merged checkout, retaining its branch and commits.

Like janitor, this requires a quiet filesystem during removal. No force,
preservation commit, recursive-delete fallback or branch deletion is used.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import ctypes
import errno
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
from pathlib import Path

from . import janitor
from . import lane_occupancy
from . import lanes
from . import store as store_mod


class Refused(ValueError):
    """A check could not establish that this checkout is disposable."""


class FenceRefused(Refused):
    """A `Refused` that comes from the store fence: the fence's own check refused a path that an activity record names
    (its `StoreFenceError` is the cause). Everything that handles a `Refused` handles it as before (the checkout is
    kept, and the text says why); the sweep (`janitor.apply`) also tells it from an ordinary "in use" refusal and stops,
    as it does for the fence's error itself. A subclass, not the fence's error re-raised: the release routes and the
    janitor command catch `Refused` and would not catch a `StoreFenceError` the same way."""


def _git(repo: Path, *args: str, exact: bool = False, data: bytes | None = None) -> tuple[int, str]:
    """(exit code, output). With ``exact`` the output is kept as Git wrote it: a carriage
    return is not turned into a line end, so a path read from it keeps every character.
    ``data`` is sent to Git's input as bytes (text mode would turn each line end into CRLF on Windows)."""
    exact = exact or data is not None
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
            capture_output=True, text=not exact, encoding=None if exact else "utf-8",
            errors=None if exact else "strict", env=env, timeout=60, input=data,
        )
        output = result.stdout.decode("utf-8") if exact else result.stdout
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise Refused(f"git could not complete the check ({type(exc).__name__})") from exc
    return result.returncode, output


def _read_git(repo: Path, *args: str) -> str:
    rc, text = _git(repo, *args)
    if rc:
        raise Refused(f"git {args[0]} failed (exit {rc}); checkout kept")
    return text.strip()


def _read_git_path(repo: Path, *args: str) -> str:
    """The one path or address a Git command prints, exactly: only the line end Git adds is
    removed, so spaces at either end stay part of it (automated comment 4236579571)."""
    rc, text = _git(repo, *args, exact=True)
    if rc:
        raise Refused(f"git {args[0]} failed (exit {rc}); checkout kept")
    return text.removesuffix("\n")


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


def _fence_git_registrations(repo: Path) -> None:
    """Before Git builds anything from every registration (its list of checkouts, a removal): Git's registration files
    are fenced (`janitor._fence_registrations`, the one check every caller uses). A file that cannot be read keeps the
    checkout (`Refused`); a refusal by the fence is the fence's own error."""
    try:
        janitor._fence_registrations(repo)
    except janitor._UseCheckFailed as exc:
        raise Refused(f"Git's registered checkouts could not be checked ({exc}); checkout kept") from exc


def _registered(repo: Path) -> dict[Path, set[str]]:
    _fence_git_registrations(repo)  # Git's list looks at every path the registrations name
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
    # Fenced at the source, like the sweep's strict reader: every reading (the main-repository check, the list of
    # paths to report or release, and each checkout's own check) refuses a path outside the store fence before anything
    # looks at it.
    for path in paths:
        store_mod.check_folder_fence(path)
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


def _refuse_alternates(objects: Path, owner: str, *, own: bool = False, name: str = "alternates") -> None:
    """Refuse a repository that borrows objects through an alternates file (or, for ``name``
    http-alternates, the one Git's web fetcher reads). How Git reads its entries (spaces,
    quoting, relative paths, links) is not repeated here, so none is trusted; the file is
    never opened. It is located first, links on the way followed, unless it is this
    repository's ``own``, which lies wherever the repository's own metadata does. An absent or
    empty file borrows nothing, and so does one below a plain file, which cannot exist."""
    listing = objects / "info" / name
    if not own:
        store_mod.check_folder_fence(listing)
    try:
        status = os.stat(listing)
    except (FileNotFoundError, NotADirectoryError):   # POSIX gives NotADirectoryError below a file
        return
    except OSError:
        store_mod.refuse_unplaced(f"{listing}, which cannot be inspected")
    if not stat.S_ISREG(status.st_mode) or status.st_size:
        store_mod.refuse_unplaced(f"{owner}, which borrows objects through {listing}")


# Files in a repository that send Git to another folder for its objects and refs (commondir)
# or name checkouts elsewhere (worktrees), from gitrepository-layout(5).
_ELSEWHERE_FILES = ("commondir", "worktrees")
# Words a repository's settings file needs to send Git elsewhere: include and includeIf read
# another settings file, a promisor remote or extensions.partialClone fetches missing objects
# from another repository, uploadpack.blobPackfileUri hands out a web address for a pack
# (git-config(1)). Settings names are spelled whole, in any case, so a file without these
# words has none of these four settings; one that only mentions them is refused too. This
# says nothing about any other setting, or about the rest of what is checked.
_ELSEWHERE_SETTINGS = (b"include", b"promisor", b"partialclone", b"blobpackfileuri")


def _refuse_links(folder: Path) -> list[Path]:
    """Refuse a folder holding anything but plain files and folders: Git follows a link or
    junction anywhere in a repository to wherever it leads. Nothing is opened or followed.
    Returns the plain files found."""
    pending, files = [folder], []
    while pending:
        place = pending.pop()
        try:
            entries = list(os.scandir(place))
        except OSError:
            store_mod.refuse_unplaced(f"git remote origin: {place}, which cannot be inspected")
        for entry in entries:
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                store_mod.refuse_unplaced(f"git remote origin: {entry.path}, which cannot be inspected")
            linked = getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            if stat.S_ISDIR(status.st_mode) and not linked:
                pending.append(Path(entry.path))
            elif not stat.S_ISREG(status.st_mode) or linked:
                store_mod.refuse_unplaced(f"git remote origin: {entry.path}, which is not a plain file or folder")
            else:
                files.append(Path(entry.path))
    return files


def _refuse_elsewhere(repository: Path) -> None:
    """Refuse a folder Git may take as origin's repository if anything in it can send Git to
    another folder or address (gitrepository-layout(5)): a .git file naming the repository,
    an alternates or http-alternates file, a commondir file or worktrees folder, a link, or
    settings that read or fetch from elsewhere. Their contents are never followed. A file
    with a second name (a hard link) passes only when all its names lie inside the fence."""
    if not repository.is_dir():
        store_mod.refuse_unplaced(f"git remote origin: {repository}, which names its repository elsewhere")
    for name in ("alternates", "http-alternates"):
        _refuse_alternates(repository / "objects", "git remote origin", name=name)
    for name in _ELSEWHERE_FILES:
        if os.path.lexists(repository / name):
            store_mod.refuse_unplaced(f"git remote origin: {repository / name}, which sends Git to another folder")
    # The walk that looks for the other names of a hard-linked file follows the links it meets inside the fence, which
    # resolves each of them: a link inside the fence that leads to an unrelated place outside was looked at (round 18).
    # Here the links are not followed (the folders they lead to inside the fence are walked as themselves).
    store_mod.check_second_names(_refuse_links(repository), follow_links=False)
    for name in ("config", "config.worktree"):
        settings = repository / name
        try:
            text = settings.read_bytes().lower() if settings.is_file() else b""
        except OSError:
            store_mod.refuse_unplaced(f"git remote origin: {settings}, which cannot be inspected")
        if any(word in text for word in _ELSEWHERE_SETTINGS):
            store_mod.refuse_unplaced(
                f"git remote origin: {settings}, whose settings can send Git to another folder or address")


def check_fence(cfg: janitor.JanitorConfig) -> str:
    """Under AGENTTALK_STORE_FENCE, before release asks any remote anything: Git may read only
    folders inside the fence beyond this repository's own metadata. That is the repository
    origin names (its effective address, after url.<base>.insteadOf, relative to the
    repository, links followed), as Git looks it up (path/.git, path, path.git/.git, path.git).
    Refused instead of followed: a partial clone (its remotes are fetched from on demand), a
    repository, here or origin, that borrows objects through an alternates file, and an origin
    with anything else that can send Git elsewhere (_refuse_elsewhere). A remote that is not a
    local folder, or one whose folder cannot be worked out, is refused too.

    Returns origin's folder as one absolute path with links followed. Release hands Git that
    exact string instead of the name origin, so what was checked is what Git uses, and
    origin's own settings (an upload-pack program among them) no longer apply. A url rule
    that would still rewrite that string is refused."""
    try:
        objects = cfg.repo / _read_git_path(cfg.repo, "rev-parse", "--git-path", "objects")
        promisors = _promisor_remotes(cfg.repo)
        url = _read_git_path(cfg.repo, "ls-remote", "--get-url", "origin")
    except Refused:
        store_mod.refuse_unplaced("this repository's Git settings, which Git could not give")
    for name in promisors:
        store_mod.refuse_unplaced(f"git remote {name}, which a partial clone fetches missing objects from")
    _refuse_alternates(objects, "this repository", own=True)
    path = _local_repository(url, cfg.repo) if url else None
    if path is None:
        store_mod.refuse_unplaced("git remote origin, which is not a local folder")
    # As written first, before anything resolves it: an origin outside the fence (a network path too) is refused
    # at once, and neither the resolver below nor the fence's own check (which resolves) is asked about it.
    for written in (path, Path(f"{path}.git")):
        if _outside_as_written(written):
            store_mod._refuse(written, Path(os.path.abspath(written)), store_mod._fence(), why="outside")
    # As configured, and as release hands it on. Git tries path/.git, path, path.git/.git, path.git.
    bases = [form for base in dict.fromkeys((path, path.resolve())) for form in (base, Path(f"{base}.git"))]
    for base in bases:
        store_mod.check_folder_fence(base)
        store_mod.check_folder_fence(base / ".git")
    for base in bases:
        for form in (base / ".git", base):
            if os.path.lexists(form):
                _refuse_elsewhere(form)
    origin = str(path.resolve())
    try:
        rewritten = _read_git_path(cfg.repo, "ls-remote", "--get-url", origin) != origin
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


# The names that `_placed` accepted as absent while one `_activity` read ran, kept so that they can be looked at again
# at the removal decision (`_still_absent`). A lookup that said "absent" is true only for that instant.
_absent_seen: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("absent_seen", default=None)


@contextlib.contextmanager
def _watching_absent():
    seen: list[str] = []
    token = _absent_seen.set(seen)
    try:
        yield seen
    finally:
        _absent_seen.reset(token)


def _still_absent(seen: list[str]) -> None:
    """Refuse when any name that was read as absent exists now. A record that named a path that was not there was
    judged unrelated from one lookup; the name can be created, as a link to the checkout, right after that lookup. So
    the read under the locks is followed by a second look at each of those names, immediately before Git removes the
    checkout. This narrows the window; it cannot close it (no check by path can: see the notes on the concurrent
    rename race), and the same window exists in `lanes._resolve_absent` for a path with one missing part."""
    for entry in seen:
        try:
            os.lstat(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Refused(
                f"a path that a record names could not be looked at again before the removal ({_why(exc)}); "
                "the checkout was kept; make that path readable, then run the release again"
            ) from exc
        raise Refused(
            "a path that a record names, and that was absent when the records were read, exists now; "
            "the checkout was kept; run the release again: it reads the records afresh and says what uses it"
        )


def _why(exc: BaseException) -> str:
    """A short reason for a refusal: what the system said, never a path."""
    return str(getattr(exc, "strerror", None) or type(exc).__name__)


def _placed(path: Path) -> str:
    """Where `path` is, as a canonical token (`lanes.canonical_host_path_checked`), resolved strictly: any failure to
    follow it (access denied, a sharing violation, a loop, a dangling link or junction, a file where a folder should
    be, an unsupported spelling) raises OSError, which the caller turns into a refusal. The tolerant `Path.resolve()`
    hands back a partial answer that still names a link when Windows cannot follow it, so a record that names the
    checkout through that link read as an unrelated path (#853).

    A path that is not there is accepted only when its absence is proved. Starting from the nearest part that exists
    (its `lstat` answers, so a link is looked at, not followed), which must resolve strictly and be a folder, the
    missing names are added to its resolved place as written: nothing that is not there can be a link. One, two or
    more missing parts are placed this way, so a record of a folder that is long gone does not hold a checkout. A
    missing name that is `.` or `..` cannot be placed and raises."""
    current = os.fspath(path)
    missing: list[str] = []
    while True:
        try:
            os.lstat(current)
            break
        except FileNotFoundError:
            # Only this platform's separators: on Linux and macOS a backslash is an ordinary character of a name,
            # and stripping it would watch `gone` for a recorded `gone\`, which a real `gone\` could then slip past.
            parent, name = os.path.split(current.rstrip(os.sep + (os.altsep or "")))
            if not name or not parent or parent == current:
                raise
            if name in (".", "..") or (os.name == "nt" and ":" in name):
                message = "cannot be placed: a missing part that is not a plain name"
                raise OSError(errno.EINVAL, message, current) from None
            missing.append(name)
            current = parent
    token = lanes.canonical_host_path_checked(current)
    if missing:
        if not stat.S_ISDIR(os.stat(current).st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "cannot be placed: the part that exists is not a folder", current)
        token = token.rstrip("/") + "/" + "/".join(os.path.normcase(name) for name in reversed(missing))
        seen = _absent_seen.get()
        if seen is not None:
            seen.append(os.path.join(current, missing[-1]))  # the name directly below the part that exists
    return token


def _outside_as_written(path: Path) -> bool:
    """Under AGENTTALK_STORE_FENCE: is `path`, made absolute as written, outside the fence both as given and as the
    fence folder itself resolves? No look at `path`: `abspath` is a string operation. (The fence folder, which no record
    or remote names, is resolved to make the comparison.) Outside a fence: False."""
    fence = os.environ.get(store_mod.STORE_FENCE_ENV)
    if not fence:
        return False
    written = Path(os.path.abspath(path))
    allowed = {Path(os.path.abspath(fence)), Path(os.path.realpath(fence))}
    return not any(written == root or root in written.parents for root in allowed)


def _fence_record_path(path: Path, key: str, source: str) -> None:
    """Under AGENTTALK_STORE_FENCE, a path that an activity record names is fenced before anything resolves it:
    `_placed` would `lstat` and `realpath` it, which for a path outside the fence reads (or, for a network path,
    contacts) what the fence exists to keep out. A path the fence refuses keeps the checkout, with the reason
    (`Refused`): it is never read as unrelated. Outside a fence nothing changes.

    Two steps. First, the path as written is compared with the fence without any look at the PATH: `abspath` is a string
    operation, so a path that is outside the fence both as given and as the fence resolves to is refused at once and is
    never resolved. (The fence folder itself, which is not named by a record, is resolved with `realpath` to make that
    comparison: step 1 is not string-only, it is free of any look at the record-named path.) A path that is inside only
    through another spelling of the fence folder (a link to it) is refused too, which only keeps a checkout. Second, a
    path inside as written goes through the fence's own check, which resolves it and so follows a link before it
    refuses one that leads out (#913)."""
    if not os.environ.get(store_mod.STORE_FENCE_ENV):
        return
    reason, fence_error = None, None
    if _outside_as_written(path):
        reason = "is outside the fence"
    else:
        try:
            store_mod.check_folder_fence(path)
        except store_mod.StoreFenceError as exc:
            reason, fence_error = "leads outside the fence", exc
    if reason is not None:
        message = (
            f"{source}: the record names a path outside the fence (activity path {key} {reason}); "
            "the checkout was kept, as nothing shows that this path is unrelated to it"
        )
        if fence_error is not None:
            # The fence's own check refused it (it resolved the path and it led outside): keep that type.
            raise FenceRefused(message) from fence_error
        raise Refused(message)  # refused from the text alone, nothing looked at: an ordinary kept checkout


_RECORD_PATH_FIELDS = ("cwd", "launch_cwd", "workspace_path", "worktree_path")


def _fence_named_fields(value: dict, repo: Path, source: str, *, lane: bool) -> None:
    """Under a store fence, fence EVERY absolute path that a record dictionary names in a field that names a folder,
    before anything else is done with the dictionary. Without this, the code that judges a damaged lane row (and the
    repair advice it prints, which looks at the row's OTHER saved path to say whether something is still there) ran
    before the check of each path and looked at a path outside the fence (round 17). One function at the top of the
    reader means that no later step, advice included, can reach a record-named absolute path that was not fenced. A
    relative path is not a path outside the fence: the existing rules judge it, and each spelling that is tried for it
    is fenced where it is placed. Outside a fence nothing is done."""
    if not os.environ.get(store_mod.STORE_FENCE_ENV):
        return
    for key in _RECORD_PATH_FIELDS + (("worktree_toplevel_canonical",) if lane else ()):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            continue
        text = item.replace("{ROOT}", str(repo))
        try:
            absolute = lanes.path_is_absolute(text)
        except (ValueError, TypeError):
            absolute = False  # not a path at all: the damage rules below refuse it, and nothing can be looked at
        if absolute:
            try:
                _fence_record_path(Path(text), key, source)
            except Refused:
                raise  # the checkout is kept, with the reason
            except ValueError:
                continue  # a name with NUL or a lone surrogate cannot be looked at either: the damage rules refuse it


def _names_checkout(value: object, target: Path, repo: Path, *, lane_id: str | None,
                    source: str = "activity record", lane: bool = False, owner: dict | None = None) -> bool:
    """True when `value` names the checkout `target`. `lane_id` is the registry key of the lane row being read (None
    for a record that is not a lane row); it is passed by name and never taken from the row, which can name another
    lane, so a recovery hint always names the row that was selected. `owner` is that live row: the first lane-level
    call sets it, and the recursion keeps it, so a saved copy inside the row (the lane snapshot of a pending delivery)
    is judged for its advice by the state of the row that holds it, not by the copy's own old state."""
    if isinstance(value, dict):
        _fence_named_fields(value, repo, source, lane=lane)  # before ANYTHING else is done with this dictionary
    if lane and owner is None and isinstance(value, dict):
        owner = value
    if lane and isinstance(value, dict):
        # One lexical rule, first: before any folder is chosen, matched or skipped (an earlier matching path, a
        # dictionary order, or an acknowledged retirement must not hide it). Only the canonical field has it.
        canonical = lanes.canonical_checkout_problem(value, root=repo)
        if canonical is not None:
            if "older version" in canonical[1]:
                recovery = lanes.checkout_path_recovery(lane_id=lane_id or "", lane=value, owner=owner, root=repo)
                raise Refused(f"{source}: activity path {canonical[0]} {canonical[1]}; "
                              f"set it to the checkout's real absolute folder; {recovery}")
            if "NUL" in canonical[1] or "surrogate" in canonical[1]:
                # keep the shared reason: it is not a relative path, it is not a path at all
                raise Refused(f"{source}: activity path {canonical[0]} {canonical[1]}; record an absolute path first")
            raise Refused(f"{source}: relative activity path {canonical[0]} is ambiguous; "
                          "record an absolute path first")
        if lanes.checkout_damage_acknowledged(value):
            # Only the acknowledged damage is retired. A surviving path still
            # names its checkout and must take the normal occupancy claim.
            # (a `worktree_path` that is non-absolute as stored, whitespace only included, is not retired: it may still
            # name a checkout, so it stays and is refused below instead of being dropped; `lane status` shows that row)
            value = {key: item for key, item in value.items()
                     if key not in {"worktree_path", "worktree_toplevel_canonical"}
                     or not lanes.acknowledgement_retires(key, item)}
        stored = {key: item.replace("{ROOT}", str(repo)) if isinstance(item, str) else item
                  for key, item in value.items()}
        problem = lanes.checkout_path_problem(stored)
        if problem is not None:
            key, reason = problem
            recovery = lanes.checkout_path_recovery(
                lane_id=lane_id if lane_id is not None else "", lane=value if isinstance(value, dict) else None,
                owner=owner, root=repo)
            if reason == "is not a string":
                raise Refused(f"{source}: could not read activity path {key}; "
                              f"{recovery}")
            if "NUL" in reason or "surrogate" in reason:
                raise Refused(f"{source}: activity path {key} {reason}; record an absolute path first; "
                              f"{recovery}")
            raise Refused(f"{source}: relative activity path {key} is ambiguous; "
                          "record an absolute path first; "
                          f"{recovery}")
    fields = set(_RECORD_PATH_FIELDS)
    if lane:
        fields.add("worktree_toplevel_canonical")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in fields and isinstance(item, str):
                path = Path(item.replace("{ROOT}", str(repo)))
                paths = [path]
                if not path.is_absolute():
                    as_stored = lane and key == "worktree_path" and lanes.worktree_path_is_relative({key: item})
                    if as_stored and not path.drive:
                        # Preserve the legacy caller-relative and repo-relative
                        # spellings; canonical provenance is checked as well.
                        paths = [repo / path, Path.cwd() / path]
                    else:
                        raise Refused(f"{source}: relative activity path {key} is ambiguous; "
                                      "record an absolute path first")
                for candidate in paths:
                    _fence_record_path(candidate, key, source)  # under a store fence: before anything looks at it
                    try:
                        where = _placed(candidate)
                    except (OSError, ValueError) as exc:
                        raise Refused(
                            f"{source}: could not establish where activity path {key} leads "
                            f"({_why(exc)}); the checkout was kept"
                        ) from exc
                    try:
                        mine = _placed(target)
                    except (OSError, ValueError) as exc:
                        raise Refused(
                            f"{source}: could not establish where the checkout is ({_why(exc)}); the checkout was kept"
                        ) from exc
                    # A child's token starts with the checkout's token and exactly one "/". The token of a root (`c:/`
                    # on a drive, `/` on Linux and macOS) already ends in that "/", so it is not added a second time
                    # (`mine + "/"` looked for `c://` and judged every folder in the drive unrelated). A token that
                    # ends in a backslash (a POSIX name) is not touched: only "/" is stripped, as `_placed` joins.
                    prefix = mine.rstrip("/") + "/"
                    if where == mine or where.startswith(prefix):
                        return True
            elif key in fields and item is not None:
                if not isinstance(item, str):
                    raise Refused(f"{source}: could not read activity path {key}")
            elif isinstance(item, (dict, list)) and _names_checkout(
                    item, target, repo, lane_id=lane_id, source=source, lane=lane, owner=owner):
                return True
    elif isinstance(value, list):
        return any(_names_checkout(item, target, repo, lane_id=lane_id, source=source, lane=lane, owner=owner)
                   for item in value)
    return False


def _free_lanes(cfg: janitor.JanitorConfig, target: Path, claim) -> list[str]:
    """Every lane that names this checkout, whatever its status (a session can outlive its lane's delivery), after
    ``claim(store, lane_id)`` has found the lane's occupancy token free."""
    def old_match(path):
        try:
            return os.name != "nt" and path.replace("{ROOT}", str(cfg.repo)) == _placed(target).replace("\\", "/")
        except (OSError, ValueError) as exc:
            raise Refused(f"could not establish where the checkout is ({_why(exc)}); the checkout was kept") from exc

    named = []
    for lane_id, row in (_activity(cfg, target) or {}).items():
        source = f"lanes.json lane {lane_id}"
        try:  # before the id names, peeks or claims anything: its token path must stay in the store
            lanes.validate_lane_id(lane_id)
        except lanes.LaneError as exc:
            raise Refused(f"{source}: {exc}; the checkout was kept") from exc
        retained = row.get("occupancy_worktrees", [])
        if not isinstance(retained, list) or any(not isinstance(p, str) or not p.strip() for p in retained):
            raise Refused(f"{source}: could not read retained occupancy worktrees")
        if any(lanes.old_backslash_path(p, root=cfg.repo) for p in retained):
            raise Refused(f"{source}: a retained occupancy worktree was saved by an older version for a folder whose "
                          "name ends in a backslash; set it to the checkout's real absolute folder")
        if _names_checkout(row, target, cfg.repo, lane_id=lane_id, source=source, lane=True) or any(
                _names_checkout({"worktree_path": p}, target, cfg.repo, lane_id=lane_id, source=source, lane=True)
                or old_match(p)
                for p in retained):
            named.append(lane_id)
    for lane_id in named:
        busy = claim(store_mod.Store(cfg.repo), lane_id)
        if busy is not None:
            raise Refused(lane_occupancy.refusal(lane_id, busy))
    return named


def _activity(cfg: janitor.JanitorConfig, target: Path) -> dict | None:
    """Refuses a checkout something is using; returns the lane records it read, if the store has any."""
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
        if _names_checkout(_read_record(cfg, path), target, cfg.repo, lane_id=None, source=path.name):
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
            if _names_checkout(row, target, cfg.repo, lane_id=lane_id, source=source, lane=True):
                raise Refused(f"an active lane names this checkout ({source})")
    # Retained launch records are conservative blockers even if their process
    # appears stopped: a configured supervisor may restart it at the next tick.
    if _names_checkout(_read_record(cfg, state / "supervisor-state.json"), target, cfg.repo, lane_id=None,
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
                if _names_checkout(row, target, cfg.repo, lane_id=None, source=f"launch-requests/{path.name}"):
                    raise Refused(f"a launch request names this checkout ({path.name})")
                source = f"lanes.json lane {lane_id}"
                if lane_id in lanes and _names_checkout(
                        lanes[lane_id], target, cfg.repo, lane_id=lane_id, source=source, lane=True):
                    raise Refused(f"a launch request names this checkout ({path.name}, {source})")
    return lanes


def check_unused(cfg: janitor.JanitorConfig, targets: list[Path]) -> None:
    """A look that changes nothing, for a listing: refuses (`Refused`, or an OSError, ValueError or TypeError for a
    record that cannot be read) when any of these registered checkouts is in use, by the same rules a release applies:
    an active, pending or launch record that names it, or a lane that names it whose occupancy token is held or cannot
    be checked."""
    for target in _in_claim_order(targets):
        _free_lanes(cfg, target, lane_occupancy.peek)  # reads the records first (`_activity`), then looks at tokens


def _in_claim_order(targets: list[Path]) -> list[Path]:
    return sorted(targets, key=lambda path: os.path.normcase(os.fspath(path)))


class RemovalGuard:
    """What a removal of registered checkouts holds, shared by a release and the ordinary sweep.

    Entering it takes a non-waiting cleanup claim on every lane that names one of the checkouts (checkouts in a fixed
    order, one claim per lane even when two checkouts name it) and refuses, releasing every claim taken, when a record
    makes any of them in use or a claim is not free. The claims are kept until the block ends, so no session can start
    on those lanes through everything that follows (a work-in-progress commit, the removal). `final_window()` is the
    last look, made at the removal itself.

    The last look (`reread()`) is the whole first look again, for a release and for the sweep: every record, ended
    lanes and their retained occupancy paths included (a session can outlive its lane, #853), a claim on every lane
    that names a checkout now and did not before, kept until the block ends, and a second look at every name that was
    read as absent. `closed_window` is for the sweep and decides only the lock: a release takes the lane writers' lock
    only when a lane named a checkout at the first look, so a record written after its last read by a program that
    does not take the lock is not seen; with `closed_window` the lock is taken whenever the store exists."""

    def __init__(self, cfg: janitor.JanitorConfig, targets: list[Path], *, closed_window: bool = False):
        self.cfg = cfg
        self.targets = _in_claim_order(targets)
        self.named: list[str] = []
        self.closed_window = closed_window
        self._claims = contextlib.ExitStack()
        self._claimed: set[str] = set()

    def _claim(self, store, lane_id):
        if lane_id in self._claimed:
            return None
        busy = self._claims.enter_context(lane_occupancy.cleanup_claim(store, lane_id))
        if busy is None:
            self._claimed.add(lane_id)
        return busy

    def _take_claims(self) -> None:
        for target in self.targets:
            self.named += [lane for lane in _free_lanes(self.cfg, target, self._claim) if lane not in self.named]

    def __enter__(self) -> "RemovalGuard":
        try:
            self._take_claims()
        except BaseException:
            self._claims.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._claims.close()

    def reread(self) -> None:
        """The last look before the removal: every record again (ended lanes and their retained occupancy paths too), a
        claim on any lane that names a checkout now, and a second look at the names that were read as absent
        (`_still_absent`)."""
        with _watching_absent() as seen:
            self._take_claims()
        _still_absent(seen)

    @contextlib.contextmanager
    def final_window(self):
        """Re-reads every record under the lock that every lane writer takes, then lets the caller remove. The token
        does not stop a lane writer; this lock, which every one of them takes, does. It is taken only here, not around
        the checks that hash every file: lane writers wait 10 seconds for it. Names read as absent are looked at once
        more before the caller goes on (`_still_absent`)."""
        if self.named:
            locked = True
        else:
            locked = self.closed_window and (self.cfg.repo / ".agenttalk").is_dir()  # no store, no lane writers
        with store_mod.Store(self.cfg.repo)._config_lock() if locked else contextlib.nullcontext():
            self.reread()  # never `_activity` alone: it skips ended lanes, whose sessions can still use a checkout
            yield


# One Git call hashes at most this many files and bytes (a file over the byte limit goes alone), and a file with a
# clean filter goes alone in the converted pass: each call keeps its own minute (`_git`'s timeout), so the check as a
# whole has no fixed limit, but no call is asked to do more than a minute's worth.
_GROUP_NAMES = 1024
_GROUP_BYTES = 256 * 1024 * 1024


def _groups(names: list[str], sizes: dict[str, int], single: frozenset[str]) -> list[list[str]]:
    """Split names, in order, into groups of one hash call each. The next name joins the group unless the group is
    not empty and adding it would go over a limit: the count, the bytes, or a count of 1 for a name in ``single``."""
    groups: list[list[str]] = []
    group: list[str] = []
    total, limit = 0, _GROUP_NAMES
    for name in names:
        size, cap = sizes[name], 1 if name in single else _GROUP_NAMES
        if group and (len(group) >= min(limit, cap) or total + size > _GROUP_BYTES):
            groups.append(group)
            group, total, limit = [], 0, _GROUP_NAMES
        group.append(name)
        total, limit = total + size, min(limit, cap)
    return groups + [group] if group else groups


def _filtered_names(target: Path, names: list[str]) -> frozenset[str]:
    """The names a clean filter may apply to, from one ``check-attr -z --stdin filter`` call (its own minute). Its
    answer must be exact: one (path, attribute, value) triple per name sent, the same paths in the same order, and a
    final NUL. Anything else refuses; it is never read as "no filters". A value other than unspecified or unset
    (set, or a driver name) counts as filtered, without running the driver. Those two words also count when a
    driver with that name is configured; one config query per content check has its own minute. With unspecified
    configured, every file with no filter attribute goes alone: in practice the whole checkout can be checked
    file by file, making release slower."""
    rc, out = _git(target, "config", "--name-only", "--get-regexp", r"^filter\.(unset|unspecified)\.[^.]+$")
    keys = out.splitlines()
    if rc == 1 and not out:
        keys = []  # Git found no matching configuration.
    elif (
        rc
        or not keys
        or any(not re.fullmatch(r"filter\.(unset|unspecified)\.[A-Za-z][A-Za-z0-9-]*", key) for key in keys)
    ):
        raise Refused(f"git config could not identify filter drivers (exit {rc}); checkout kept")
    drivers = {key.split(".")[1] for key in keys}
    rc, out = _git(target, "check-attr", "-z", "--stdin", "filter", data="".join(f"{n}\0" for n in names).encode())
    fields = out[:-1].split("\0") if out.endswith("\0") else []
    exact = len(fields) == 3 * len(names) and fields[0::3] == names and set(fields[1::3]) == {"filter"}
    if rc or not exact:
        raise Refused(f"git check-attr failed or gave an answer that does not match the files (exit {rc})")
    return frozenset(
        n
        for n, value in zip(names, fields[2::3], strict=True)
        if value not in ("unspecified", "unset") or value in drivers
    )


def _file_metadata(target: Path, names: list[str]) -> dict[str, tuple[int, int]]:
    """Each file's size and modification time, by one metadata read (never its content). A failed read
    refuses: never a size of 0, never a skipped file."""
    snapshots = {}
    for name in names:
        try:
            info = (target / name).lstat()
            snapshots[name] = (info.st_size, info.st_mtime_ns)
        except OSError as exc:
            raise Refused(f"could not read the size of a tracked file ({type(exc).__name__}): {name}") from exc
    return snapshots


def _hash_files(
    target: Path, names: list[str], sizes: dict[str, int], single: frozenset[str], *flags: str
) -> dict[str, str]:
    """Git's hash of each named file, one call per group (see ``_groups``). Git cannot read every name from its input,
    so each of these is asked alone, with the name as a plain argument: one holding a line end (it strips a final
    carriage return and splits at a line feed) and one starting with a double quote (it reads that as a quoted path and
    hashes a different file). No other character is read differently (checked with Git 2.53)."""
    batch = [name for name in names if not name.startswith('"') and "\r" not in name and "\n" not in name]
    hashes: dict[str, str] = {}
    for group in _groups(batch, sizes, single):
        rc, out = _git(target, "hash-object", *flags, "--stdin-paths", data="".join(f"{n}\n" for n in group).encode())
        answers = out.splitlines()
        if rc or len(answers) != len(group):
            raise Refused(f"git hash-object failed or gave an answer that does not match the files (exit {rc})")
        hashes.update(zip(group, answers, strict=True))
    for name in names:
        if name not in hashes:
            hashes[name] = _read_git(target, "hash-object", *flags, "--", name)
    return hashes


def _check_tracked_content(target: Path) -> None:
    # Status can trust cached size/time even after an edit. Hash every tracked file's content itself: first with
    # Git's clean conversion (CRLF rules), then, for the files that differ, as stored on disk. A file Git itself
    # keeps as CRLF reads as unchanged only the second way, and equal to the stored copy means it is that copy.
    rc, listing = _git(target, "ls-files", "--stage", "-z", exact=True)
    if rc:
        raise Refused(f"git ls-files failed (exit {rc}); checkout kept")
    expected: dict[str, str] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if stage != "0" or mode not in {"100644", "100755"}:
            raise Refused(f"tracked entry is not a plain file: {name}")
        expected[name] = blob
    names = list(expected)
    if not names:
        return
    filtered = _filtered_names(target, names)
    snapshots = _file_metadata(target, names)
    sizes = {name: snapshot[0] for name, snapshot in snapshots.items()}
    converted = _hash_files(target, names, sizes, filtered)
    differing = [name for name in names if converted[name] != expected[name]]
    raw = _hash_files(target, differing, sizes, frozenset(), "--no-filters") if differing else {}
    for name in differing:
        if raw[name] != expected[name]:
            raise Refused(f"tracked file content differs from the index: {store_mod.escape_controls(name)}")
    # A file checked in an early group may change while later groups are still running.
    for name, before in snapshots.items():
        try:
            info = (target / name).lstat()
        except OSError as exc:
            raise Refused(
                f"could not re-check a tracked file ({type(exc).__name__}): {store_mod.escape_controls(name)}"
            ) from exc
        if (info.st_size, info.st_mtime_ns) != before:
            raise Refused(f"tracked file changed during the content check: {store_mod.escape_controls(name)}")


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
        failed = 0  # the report's checks that broke with an unexpected error (#909)
        for path in paths:
            try:
                _unchanged(ids)
                candidate_ids = _identities(cfg, path)
                _unchanged(ids)
                size, head = _check_worktree(cfg, path, default)
                if target is None:
                    check_unused(cfg, [path])
                    _unchanged(candidate_ids)
                    lines.append(f"WOULD RELEASE {path}: {size} bytes")
                    continue
                _unchanged(candidate_ids)
                with RemovalGuard(cfg, [path]) as guard:  # claims kept through the removal: no session can start
                    # Re-read mutable eligibility, then recheck identities at the
                    # deletion boundary. No stale report can authorize a removal.
                    _, current_head = _check_worktree(cfg, path, default)
                    if current_head != head:
                        raise Refused("checkout HEAD changed during the checks")
                    _unchanged(candidate_ids)
                    _unchanged(ids)
                    with guard.final_window():
                        _fence_git_registrations(cfg.repo)  # the removal reads every registration too
                        _read_git(cfg.repo, "worktree", "remove", "--", str(path))
                lines.append(f"REMOVED {path}: branch and commits kept")
            except (Refused, OSError, ValueError, TypeError) as exc:
                if target is None and not isinstance(exc, Refused):  # a check that broke: not an ordinary keep
                    failed += 1
                    exc = janitor._could_not_check(exc)
                lines.append(f"KEPT {path}: {exc}")
                if target is not None:
                    return 1, "\n".join(store_mod.report_line(line) for line in lines)
        if failed:
            lines.append(f"could not check: {failed}")
        return (1 if failed else 0), "\n".join(store_mod.report_line(line) for line in lines)
    except (Refused, OSError, ValueError, TypeError) as exc:
        return 1, store_mod.report_line(f"FAILED: {exc}; nothing released")
