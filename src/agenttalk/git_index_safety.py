"""Read-only checks for work that status and parent WIP commits can miss."""

from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess  # nosec B404

from agenttalk import printed_commands as pc

HIDDEN = (
    "Git is skipping checks on tracked files; save or inspect them before clearing assume-unchanged or skip-worktree"
)
UNKNOWN = "could not check whether Git is hiding tracked files"
SUBMODULE = (
    "checkout contains a materialized submodule; save its work separately and remove or deinitialize it "
    "yourself before retrying"
)
SUBMODULE_UNKNOWN = "could not check whether this checkout contains a materialized submodule"
SUBMODULE_NOT_CLEAN = "a submodule is not verifiably clean; inspect its files and Git index"
# The variables that make Git work on another repository (or index) than the one agenttalk names with `-C` (#942).
GIT_REPOSITORY_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def git_repository_override(environ=os.environ) -> str | None:
    """The first of them that is present (even when empty: Git then fails or reads another place), or None."""
    return next((name for name in GIT_REPOSITORY_OVERRIDES if name in environ), None)


def git_override_text(name: str) -> str:  # why nothing was done; it names the variable only, never its value
    return (f"{name} is set, so Git would work on the repository it names instead of the store's own; nothing was "
            f"read or changed. Git sets it for commands run from its hooks: run agenttalk outside the hook, or clear "
            f"{name}, then try again")


STATUS_ARGS = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "status",
    "--porcelain",
    "--untracked-files=normal",
    "--ignore-submodules=none",
]


@dataclass(frozen=True)
class Inspection:
    state: str
    reason: str | None = None


def _checkout_root(root, *, env=None):
    command = ["git", "-C", str(root), "rev-parse", "--show-toplevel"]
    # Fixed read-only Git command, with no shell or user-selected operation.
    result = subprocess.run(command, capture_output=True, timeout=15, env=env)  # noqa: S603  # nosec B603
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or not result.stdout.endswith(b"\n"):
        raise ValueError("checkout root read failed")
    raw = result.stdout[:-1]
    name = os.fsdecode(raw)
    checkout = Path(name)
    if not raw or b"\0" in raw or os.fsencode(name) != raw or not checkout.is_absolute():
        raise ValueError("invalid checkout root")
    return checkout


def _records(root, option, *, env=None):
    command = ["git", "-C", str(root), "ls-files", "--cached", option, "-z", "--full-name"]
    # Fixed read-only Git command, with no shell or user-selected operation.
    result = subprocess.run(  # noqa: S603  # nosec B603
        command,
        capture_output=True,
        timeout=15,
        env=env,
    )
    if result.returncode != 0 or not isinstance(result.stdout, bytes):
        raise ValueError("index read failed")
    output = result.stdout
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise ValueError("incomplete index listing")
    return output[:-1].split(b"\0")


def _path(raw):
    name = os.fsdecode(raw)
    if (
        os.fsencode(name) != raw
        or not name
        or ntpath.splitdrive(name)[0]
        or name.startswith(("/", "\\"))
        or (os.name == "nt" and ":" in name)
    ):
        raise ValueError("invalid index path")
    parts = re.split(r"[\\/]" if os.name == "nt" else "/", name)
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("escaping index path")
    return Path(*parts)


def _plain_directory(info):
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _entry(root, relative):
    """Absence is safe only below verified plain directory prefixes."""
    current = Path(root)
    if not _plain_directory(os.lstat(current)):
        raise ValueError("uncertain checkout root")
    parts = relative.parts
    for number, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # Every earlier prefix was proved to be a real directory. In particular,
            # Windows WinError 3 below a plain file cannot reach this exception.
            return None
        if number == len(parts) - 1:
            return info
        if not _plain_directory(info):
            raise ValueError("uncertain index path ancestry")
    raise ValueError("empty index path")


def check_hidden_index(root, *, env=None, _root_verified=False) -> Inspection:
    try:
        root = Path(root) if _root_verified else _checkout_root(root, env=env)
        hidden = False
        for record in _records(root, "-v", env=env):
            if len(record) < 3 or record[1:2] != b" " or record[:1] not in [bytes([c]) for c in b"HSMRCK?hsmrck"]:
                raise ValueError("invalid index tag")
            tag, path = record[:1], _path(record[2:])
            if tag in [bytes([c]) for c in b"hsmrck"]:
                hidden = True
            elif tag == b"S" and _entry(root, path) is not None:
                hidden = True
        return Inspection("hidden", HIDDEN) if hidden else Inspection("safe")
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return Inspection("unverifiable", UNKNOWN)


def check_materialized_submodules(root) -> Inspection:
    try:
        root = _checkout_root(root)
        hidden = False
        for record in _records(root, "--stage"):
            header, separator, raw = record.partition(b"\t")
            if not separator or not re.fullmatch(
                rb"(?:100644|100755|120000|160000) [0-9a-f]{40}(?:[0-9a-f]{24})? [0-3]", header
            ):
                raise ValueError("invalid staged index record")
            path = _path(raw)
            if not header.startswith(b"160000 "):
                continue
            info = _entry(root, path)
            if info is None:
                continue
            if not _plain_directory(info):
                hidden = True
            else:
                with os.scandir(Path(root) / path) as entries:
                    if next(entries, None) is not None:
                        hidden = True
        return Inspection("hidden", SUBMODULE) if hidden else Inspection("safe")
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return Inspection("unverifiable", SUBMODULE_UNKNOWN)


def check_close_submodules(root, *, env=None, _root_verified=False) -> Inspection:
    """Check nested indexes and refuse content in uninitialized submodules."""
    try:
        pending = [Path(root) if _root_verified else _checkout_root(root, env=env)]
        while pending:
            checkout = pending.pop()
            for record in _records(checkout, "--stage", env=env):
                header, separator, raw = record.partition(b"\t")
                if not separator or not re.fullmatch(
                    rb"(?:100644|100755|120000|160000) [0-9a-f]{40}(?:[0-9a-f]{24})? [0-3]", header
                ):
                    raise ValueError("invalid staged index record")
                relative = _path(raw)
                if not header.startswith(b"160000 "):
                    continue
                info = _entry(checkout, relative)
                if info is None:
                    continue
                if not _plain_directory(info):
                    return Inspection("hidden", SUBMODULE_NOT_CLEAN)
                folder = checkout / relative
                try:
                    metadata = os.lstat(folder / ".git")
                except FileNotFoundError:
                    with os.scandir(folder) as entries:
                        if next(entries, None) is not None:
                            return Inspection("hidden", SUBMODULE_NOT_CLEAN)
                    continue
                if stat.S_ISLNK(metadata.st_mode) or (
                    getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise ValueError("uncertain submodule metadata")
                if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    raise ValueError("invalid submodule metadata")
                if not os.path.samefile(_checkout_root(folder, env=env), folder):
                    raise ValueError("submodule is not its own checkout")
                if check_hidden_index(folder, env=env).state != "safe":
                    return Inspection("hidden", SUBMODULE_NOT_CLEAN)
                # Fixed read-only Git command, with no shell or user-selected operation.
                result = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
                    ["git", "-C", str(folder), *STATUS_ARGS], capture_output=True, timeout=15, env=env
                )
                if result.returncode != 0 or not isinstance(result.stdout, bytes):
                    raise ValueError("submodule status failed")
                if result.stdout:
                    return Inspection("hidden", SUBMODULE_NOT_CLEAN)
                pending.append(folder)
        return Inspection("safe")
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return Inspection("unverifiable", SUBMODULE_NOT_CLEAN)


def keep_reason(root) -> str | None:
    flags = check_hidden_index(root)
    return flags.reason or check_materialized_submodules(root).reason


def inspection_guidance(folder, *, uncertain=False) -> str:
    try:
        folder = _checkout_root(folder)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        templates = ["git -C {folder} rev-parse --show-toplevel"]
        uncertain = True
    else:
        templates = ["git -C {folder} ls-files -v"]
    if not uncertain:
        templates += [
            "git -C {folder} update-index --no-assume-unchanged -- <file>",
            "git -C {folder} update-index --no-skip-worktree -- <file>",
        ]
    commands, note = pc.cmds(
        templates,
        folder=str(folder),
        shown_limit=pc.PATH_SHOWN_MAX_CHARACTERS,
        source="is the local folder being inspected",
        lookup="use the full checkout path from this operation; select a file only after inspection",
    )
    text = (
        f"Checkout folder: {pc.line(str(folder))}. First save or inspect the work. "
        "List the files: lowercase tags mean assume-unchanged, "
        "and S means skip-worktree. "
    )
    if not uncertain:
        text += "Lowercase s means both. Clear only the flag you have inspected, then retry. "
    if "--show-toplevel" in templates[0]:
        text += "Resolve the checkout top-level first, then use that folder to inspect the index. "
    text += "Replace the placeholders before running a template. " + "; ".join(f"`{command}`" for command in commands)
    return text + (f" ({note})" if note else "")
