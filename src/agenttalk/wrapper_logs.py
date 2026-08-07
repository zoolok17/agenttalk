"""Bounded wrapper process logs and factual lifecycle diagnostics.

``agenttalk wrap`` attempts capture as its first post-dispatch startup action on
every launch path. A generated supervisor may preallocate and redirect a
generation before Python starts; otherwise the wrapper allocates a generation
itself and mirrors the original console while it keeps both files bounded. The
lifecycle JSONL is diagnostic output only: it is never read by the supervisor
and carries no health or restart authority.

The bound is Python-level only: it wraps ``sys.stdout``/``sys.stderr`` and
bounds whatever text is written through those objects.  A write that reaches
file descriptor 1/2 directly - bypassing the Python stream objects entirely -
is not intercepted. No such writer exists in this project today (no third-party
dependencies, no direct ``os.write(1/2, ...)`` call, and the wrapped model
child's own stdout/stderr are piped rather than inherited), so the gap is
tracked but not yet closed.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import io
import json
import os
import re
import secrets
import signal
import shutil
import stat
import sys
import tempfile
import threading
import traceback
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from agenttalk import _atomic


ENV_STDOUT_PATH = "AGENTTALK_WRAPPER_STDOUT_LOG"
ENV_STDERR_PATH = "AGENTTALK_WRAPPER_STDERR_LOG"
ENV_MAX_BYTES = "AGENTTALK_WRAPPER_LOG_MAX_BYTES"
ENV_SEGMENT_COUNT = "AGENTTALK_WRAPPER_LOG_SEGMENTS"
ENV_LAUNCH_NONCE = "AGENTTALK_WRAPPER_LOG_NONCE"

WRAPPER_LOG_GENERATIONS = 4
WRAPPER_LOG_MAX_BYTES = 1024 * 1024
WRAPPER_LOG_SEGMENT_COUNT = 4
_MIN_MAX_BYTES = 4 * 1024
_MAX_MAX_BYTES = 64 * 1024 * 1024
_MIN_SEGMENTS = 2
_MAX_SEGMENTS = 32
_LOCATION_SCHEMA_VERSION = 1
_GENERATION_NAME_RE = re.compile(
    r"[0-9]{8}T[0-9]{9}Z-[0-9a-f]{32}\Z"
)
_SEQUENCE_RECORD_RE = re.compile(r"[1-9][0-9]*\Z")
_SEQUENCE_MAX = (1 << 63) - 1
_SEQUENCE_UNCERTAINTY_BY_STATE = {
    "missing-legacy": False,
    "missing-write-failed": True,
    "present-valid": False,
    "present-invalid": True,
}

_RUNTIME_FIELDS = (
    "phase",
    "turn_generation",
    "turn_id",
    "message_id",
    "cli_launcher_pid",
    "progress_sequence",
    "last_progress_at",
    "last_outcome",
)
_TRANSITION_EVENTS = {
    "idle": "waiting_for_mail",
    "starting": "turn_started",
    "active": "child_spawned",
    "terminal": "turn_ended",
    "dead_letter": "message_dead_lettered",
}


def _authenticated_environment(
    environ: Mapping[str, str],
    expected_nonce: str | None,
) -> bool:
    return bool(
        isinstance(expected_nonce, str)
        and len(expected_nonce) == 32
        and all(char in "0123456789abcdef" for char in expected_nonce)
        and environ.get(ENV_LAUNCH_NONCE) == expected_nonce
        and environ.get(ENV_STDOUT_PATH)
        and environ.get(ENV_STDERR_PATH)
    )


def _home_state_root(home_env: str | None, *parts: str) -> Path | None:
    """Resolve a home-relative state directory, tolerating an unresolvable
    home (no HOME/USERPROFILE and no passwd entry) by treating it as an
    unavailable candidate rather than raising."""
    try:
        home = Path(home_env) if home_env else Path.home()
        # Preserve the operator-supplied ancestry until the candidate guard
        # has inspected it. Resolving here would erase a symlink/junction and
        # make the later refusal check inspect only its target.
        return home.expanduser().absolute().joinpath(*parts)
    except (OSError, RuntimeError):
        return None


def _candidate_state_roots(
    configured: Path | None,
    home_env: str | None,
    home_parts: tuple[str, ...],
    project: Path,
) -> Iterator[Path]:
    # Each candidate is resolved only once the previous one has been tried
    # and rejected, so an unresolvable home fallback is never even attempted
    # - let alone allowed to raise - while a valid configured path (the
    # preferred input) is still available and unconsulted.
    if configured is not None and configured.is_absolute():
        yield configured
    home_root = _home_state_root(home_env, *home_parts)
    if home_root is not None:
        yield home_root
    yield Path(tempfile.gettempdir()).absolute()
    yield project.parent / ".agenttalk-wrapper-logs"


def _diagnostic_project_root(
    project_root: str | os.PathLike[str],
) -> Path:
    """Return a stable absolute project hint without making capture fragile.

    Canonical resolution is preferred and preserves the established project
    hash. If the operational root later proves unresolvable, logging must still
    have a destination for that diagnostic, so use the lexical absolute path as
    a fail-soft identity. Authoritative project validation remains the store's
    job after the bounded streams are live.
    """
    raw = Path(project_root).expanduser()
    try:
        return raw.resolve()
    except (OSError, RuntimeError):
        return raw.absolute()


def _diagnostic_project_id(project: Path) -> str:
    # project is already canonical when canonicalization is available. This is
    # byte-for-byte project_id_for_root's documented hash without a second,
    # fallible resolve between capture allocation and stream installation.
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()


def _wrapper_log_env_inputs(
    target: str,
    env: Mapping[str, str],
) -> tuple[Path | None, str | None, tuple[str, ...]]:
    if target == "nt":
        raw = env.get("LOCALAPPDATA")
        home_env, home_parts = env.get("USERPROFILE"), ("AppData", "Local")
    else:
        raw = env.get("XDG_STATE_HOME")
        home_env, home_parts = env.get("HOME"), (".local", "state")
    # A relative state-directory value is interpreted against the process cwd.
    # The supervisor must not let ambient cwd move diagnostic logs back into the
    # checkout, so only absolute platform state roots are eligible.
    configured = Path(raw) if raw else None
    if configured is not None and not configured.is_absolute():
        configured = None
    return configured, home_env, home_parts


def default_wrapper_log_root(
    project_root: str | os.PathLike[str],
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the preferred per-user, per-project log root outside the checkout.

    This is a lazy candidate, not a validated one: it is baked into the
    generated supervisor launcher and independently recomputed by the
    allocator's own candidate search. Neither consumer trusts it blindly -
    both refuse a reparse/symlink ancestor at the point they actually try to
    use the root, so this function does not perform that (filesystem-probing)
    refusal itself. Doing so here would make this report indistinguishable
    from what the allocator actually accepted, hiding a refusal instead of
    proving one happened.

    A project rooted at a filesystem anchor (``/`` on POSIX, usually a drive
    root on Windows) has no same-volume "outside": every absolute candidate
    resolves as relative to the project, so the loop below never accepts one.
    Falls back to a fixed temp-based path rather than raising in that case -
    restores a guarantee an earlier revision of this function had and later
    silently lost (#113 review), not a new one. Every reachable caller of
    this function currently treats a raised OSError as fatal (see
    ``_marker_placeholder_bundle``, called from ``supervise --init``/
    ``--refresh-scripts``), so exhausting candidates here previously took
    supervisor scaffolding down entirely for this one edge case.
    """
    env = os.environ if environ is None else environ
    target = os.name if platform is None else platform
    configured, home_env, home_parts = _wrapper_log_env_inputs(target, env)
    project = _diagnostic_project_root(project_root)
    project_id = _diagnostic_project_id(project)
    for candidate in _candidate_state_roots(configured, home_env, home_parts, project):
        try:
            raw_result = (
                candidate.expanduser().absolute()
                / "agenttalk"
                / "wrapper-logs"
                / project_id
            )
            resolved = raw_result.resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(project):
            return raw_result
    return Path(tempfile.gettempdir()).absolute() / "agenttalk" / "wrapper-logs" / project_id


def wrapper_log_root_candidates(
    project_root: str | os.PathLike[str],
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return eligible roots in launcher-compatible failover order.

    The first item is the same lazy preferred candidate
    :func:`default_wrapper_log_root` would report, filtered for reparse/symlink
    safety here since this tuple is what the allocator actually attempts. The
    second is the exact temporary fallback baked into generated PowerShell
    launchers, and the final candidate is independent of user state. Resolving
    any later candidate is fail-soft and can never invalidate an
    already-viable preferred root.

    A project rooted at a filesystem anchor has no same-volume "outside", so
    every one of the candidates above can end up relative to the project and
    get filtered out - restores the same guaranteed last-resort fallback
    :func:`default_wrapper_log_root` restores, rather than returning an empty
    tuple and letting the allocator report total failure for this edge case.
    """
    env = os.environ if environ is None else environ
    target = os.name if platform is None else platform
    configured, home_env, home_parts = _wrapper_log_env_inputs(target, env)
    project = _diagnostic_project_root(project_root)
    project_id = _diagnostic_project_id(project)
    preferred: Path | None = None
    for candidate in _candidate_state_roots(configured, home_env, home_parts, project):
        try:
            raw_result = (
                candidate.expanduser().absolute()
                / "agenttalk"
                / "wrapper-logs"
                / project_id
            )
            if _has_reparse_or_symlink_component(raw_result):
                continue
            resolved = raw_result.resolve()
            # Recheck both identities after canonicalization. The raw check
            # catches an ancestor swap; the resolved check keeps the existing
            # refusal for unsafe targets.
            if (
                _has_reparse_or_symlink_component(raw_result)
                or _has_reparse_or_symlink_component(resolved)
            ):
                continue
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(project):
            preferred = raw_result
            break

    candidate_factories = [
        lambda: preferred,
        # Keep this byte-for-byte compatible with $WrapperLogFallbackRoot in
        # the generated launcher so direct and supervised retention scan the
        # same temporary pool.
        lambda: temporary_wrapper_log_root(project),
        lambda: project_parent_wrapper_log_root(project),
    ]
    seen: set[Path] = set()
    roots: list[Path] = []
    for candidate_factory in candidate_factories:
        try:
            candidate = candidate_factory()
            if candidate is None:
                continue
            raw_candidate = candidate.expanduser().absolute()
            if _has_reparse_or_symlink_component(raw_candidate):
                continue
            resolved = raw_candidate.resolve()
            if (
                _has_reparse_or_symlink_component(raw_candidate)
                or _has_reparse_or_symlink_component(resolved)
            ):
                continue
        except (OSError, RuntimeError):
            continue
        if resolved in seen or resolved.is_relative_to(project):
            continue
        seen.add(resolved)
        # Allocation must retain the ancestry that was validated. Returning
        # only the canonical target would make a junction disappear before
        # _prepare_agent_log_dir's pre/post-create race checks can see it.
        roots.append(raw_candidate)

    if not roots:
        # A checkout rooted at the filesystem anchor has no same-volume
        # "outside" - every candidate above was filtered for being relative
        # to the project. Keep wrapping usable; allocation remains fail-soft
        # if this cannot open either. (Restores a guarantee this function
        # had before it was silently dropped; #113 review.)
        roots.append(
            Path(tempfile.gettempdir()).absolute()
            / "agenttalk"
            / "wrapper-logs"
            / project_id
        )
    return tuple(roots)


def _wrapper_log_agent_leaf(agent: str) -> str:
    digest = hashlib.sha256(agent.encode("utf-8")).hexdigest()
    return f"agent-{digest[:16]}"


def temporary_wrapper_log_root(
    project_root: str | os.PathLike[str],
) -> Path:
    project = _diagnostic_project_root(project_root)
    return (
        Path(tempfile.gettempdir()).absolute()
        / "agenttalk-wrapper-logs"
        / _diagnostic_project_id(project)
    )


def project_parent_wrapper_log_root(
    project_root: str | os.PathLike[str],
) -> Path:
    project = _diagnostic_project_root(project_root)
    return (
        project.parent
        / ".agenttalk-wrapper-logs"
        / "agenttalk"
        / "wrapper-logs"
        / _diagnostic_project_id(project)
    )


@dataclass(frozen=True)
class WrapperLogInstallation:
    enabled: bool
    confirmed: bool = False
    root: Path | None = None
    generation_dir: Path | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    # Populated only when enabled is False because allocation itself was
    # attempted and rejected every candidate (see WrapperLogAllocationFailed).
    # Stays None for the other disabled cases (unauthenticated supervised
    # environment, no project/agent to allocate for) where there is no
    # single failure to name.
    disabled_reason: str | None = None
    # Candidates tried and rejected BEFORE the accepted root, when enabled is
    # True. A partial failure (preferred root rejected, a fallback quietly
    # accepted) is exactly as silent as total failure was before
    # disabled_reason existed unless this is surfaced too - this is that.
    rejected_attempts: tuple[tuple[Path, str], ...] = ()
    # True only when `root`/`rejected_attempts` came from actually running the
    # fallback-search allocator (the direct/unsupervised launch path). False
    # for the pre-authenticated supervised path, where the supervisor already
    # resolved fixed stdout/stderr paths and `root` is not a meaningful
    # candidate-search result - callers must not print or otherwise surface
    # `root`/`rejected_attempts` as allocator diagnostics unless this is True.
    allocated_via_fallback_search: bool = False


@dataclass(frozen=True)
class _AllocatedWrapperLogTargets:
    root: Path
    generation_dir: Path
    stdout_path: Path
    stderr_path: Path
    roots: tuple[Path, ...]
    agent_leaf: str
    sequence_uncertain: bool
    rejected_attempts: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True)
class _ActiveGenerationLock:
    path: Path
    fd: int
    identity: os.stat_result


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _utf8_prefix(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", "ignore")


def _utf8_chunk_boundary(raw, limit: int) -> int:
    """Largest n <= min(limit, len(raw)) such that raw[:n] does not end
    mid-code-point. Assumes raw is itself well-formed UTF-8 as a whole (as
    produced by str.encode("utf-8", ...)), not that every prefix of it is -
    only the last, possibly-truncated code point within the limit matters."""
    n = min(limit, len(raw))
    if n <= 0:
        return 0
    start = n - 1
    while start > 0 and (raw[start] & 0xC0) == 0x80:
        start -= 1
    lead = raw[start]
    if lead < 0x80:
        seq_len = 1
    elif lead & 0xE0 == 0xC0:
        seq_len = 2
    elif lead & 0xF0 == 0xE0:
        seq_len = 3
    elif lead & 0xF8 == 0xF0:
        seq_len = 4
    else:
        seq_len = 1
    return n if start + seq_len <= n else start


def _harden_posix_log_paths(*paths: Path) -> None:
    if os.name == "nt":
        return
    directories: set[Path] = set()
    for path in paths:
        directories.add(path.parent)
        directories.add(path.parent.parent)
        directories.add(path.parent.parent.parent)
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    for directory in directories:
        with contextlib.suppress(OSError):
            os.chmod(directory, stat.S_IRWXU)


def _restrictive_file_opener(path: str, flags: int) -> int:
    """An io.open() opener that bakes 0600 into the underlying os.open()
    call - the mode argument only applies when the file is actually being
    created, so this closes the create-then-chmod window without changing
    behavior for a file that already exists."""
    return os.open(path, flags, 0o600)


# region wrapper-log-retention
_WIN32_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_WIN32_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _windows_raw_file_attributes(path: Path) -> int | None:
    """Return the raw Win32 file attributes for `path` via
    GetFileAttributesW, bypassing os.lstat()'s populated
    st_file_attributes field entirely.

    #113 review, round 5: a real OneDrive placeholder file reports
    FILE_ATTRIBUTE_REPARSE_POINT to this exact WinAPI call (the same one
    PowerShell's Get-Item/.Attributes uses) but Python's os.lstat() does
    not reflect it on this host - the two languages' classifiers
    disagreed about the identical input, which this scan exists to
    prevent. Returns None on a non-Windows platform or if the call
    itself fails (an ambiguous result here must not silently downgrade
    to "definitely not a reparse point" - the caller ORs this in as an
    additional positive signal, never a replacement one, so a None here
    just means "no additional information," not "confirmed clear").
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        result = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
    except (OSError, AttributeError, ValueError):
        return None
    if result == _WIN32_INVALID_FILE_ATTRIBUTES:
        return None
    return result


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OSError(f"cannot validate wrapper log path component: {path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or bool(file_attributes & reparse_flag):
        return True
    windows_attributes = _windows_raw_file_attributes(path)
    if windows_attributes is not None:
        return bool(windows_attributes & _WIN32_FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _has_reparse_or_symlink_component(path: Path) -> bool:
    current = path.absolute()
    while True:
        if _is_reparse_or_symlink(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _prepare_agent_log_dir(root: Path, agent_leaf: str) -> Path:
    # This first check runs BEFORE creation - root may genuinely not
    # exist yet on first use, which _has_reparse_or_symlink_component
    # already treats as "nothing unsafe here yet" at each ancestor
    # level. _scan_path is the wrong classifier here: it requires a
    # CONFIRMED present directory, which would wrongly reject an
    # ordinary not-yet-created root. Left as the raw ancestry primitive
    # deliberately - not a site the shared directory classifier fits.
    if _has_reparse_or_symlink_component(root):
        raise OSError(f"unsafe wrapper log root ancestry: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # These two checks run AFTER creation, where the directory is
    # expected to exist - routed through the shared classifier (#113
    # review, round 5 sweep). PLAIN_DIRECTORY already implies both "is a
    # directory" and "no reparse/symlink ancestry" (_scan_path calls
    # _has_reparse_or_symlink_component internally), dropping the
    # now-redundant standalone ancestry call. Behavior is unchanged: a
    # swallowed-ambiguous result and a confirmed-unsafe result both
    # raised OSError before (rejecting this candidate), and both still
    # do - only the immediate raise site and message text differ, not
    # the caller-visible outcome.
    if _scan_path(root) is not _PathScanOutcome.PLAIN_DIRECTORY:
        raise OSError(f"unsafe wrapper log root: {root}")
    agent_dir = root / agent_leaf
    agent_dir.mkdir(mode=0o700, exist_ok=True)
    if _scan_path(agent_dir) is not _PathScanOutcome.PLAIN_DIRECTORY:
        raise OSError(f"unsafe wrapper log agent directory: {agent_dir}")
    if os.name != "nt":
        os.chmod(root, stat.S_IRWXU)
        os.chmod(agent_dir, stat.S_IRWXU)
    return agent_dir


def _active_generation_lock_path(generation_dir: Path) -> Path:
    """Return a lock path outside the generation so it can guard deletion."""
    return generation_dir.parent / f".{generation_dir.name}.active"


def _validate_open_lock_path(path: Path, fd: int) -> None:
    opened = os.fstat(fd)
    current = os.lstat(path)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or _is_reparse_or_symlink(path)
    ):
        raise OSError(f"unsafe wrapper log active lock: {path}")


def _try_lock_active_fd(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.fstat(fd).st_size < 1:
        os.write(fd, b"\0")
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            # FileStream.Lock uses a byte-range record lock on Unix. lockf,
            # rather than flock, keeps the Python and generated-PowerShell
            # pruners on the same cross-language primitive.
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
        return True
    except OSError:
        return False


def _unlock_active_fd(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.lockf(fd, fcntl.LOCK_UN, 1)


def _acquire_active_generation_lock(
    generation_dir: Path,
) -> _ActiveGenerationLock | None:
    path = _active_generation_lock_path(generation_dir)
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    created = False
    created_identity: os.stat_result | None = None
    try:
        fd = os.open(str(path), flags, 0o600)
        created = True
        created_identity = os.fstat(fd)
        _validate_open_lock_path(path, fd)
        if not _try_lock_active_fd(fd):
            raise OSError(f"could not acquire wrapper log active lock: {path}")
        return _ActiveGenerationLock(
            path=path,
            fd=fd,
            identity=created_identity,
        )
    except OSError:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if created and created_identity is not None:
            with contextlib.suppress(OSError):
                _unlink_active_lock_if_same(path, created_identity)
        return None


def _unlink_active_lock_if_same(path: Path, identity: os.stat_result) -> None:
    # Reuse the store's pathname-generation-safe removal. Closing a lock before
    # unlinking is required on Windows, but a replacement must never be removed
    # in the close/unlink race on either platform.
    from agenttalk.store import _unlink_if_same_file

    _unlink_if_same_file(path, identity)


def _release_active_generation_lock(lock: _ActiveGenerationLock) -> None:
    try:
        _unlock_active_fd(lock.fd)
    finally:
        os.close(lock.fd)
    with contextlib.suppress(OSError):
        _unlink_active_lock_if_same(lock.path, lock.identity)


@contextlib.contextmanager
def _guard_wrapper_log_prune(generation_dir: Path) -> Iterator[bool]:
    """Hold an inactive generation's byte lock through its deletion attempt."""
    path = _active_generation_lock_path(generation_dir)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        yield True
        return
    except OSError:
        yield False
        return
    acquired = False
    identity: os.stat_result | None = None
    try:
        _validate_open_lock_path(path, fd)
        identity = os.fstat(fd)
        acquired = _try_lock_active_fd(fd)
    except OSError:
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                _unlock_active_fd(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        # Routed through the shared classifier (#113 review, round 5
        # sweep) - a real behavior change, not just a relabeling: only a
        # CONFIRMED absence should trigger cleaning up this now-orphaned
        # lock file. The prior .exists() swallowed an unreadable
        # generation_dir into "gone", which could remove the lock
        # protecting a generation that might still genuinely be there.
        if (
            acquired
            and identity is not None
            and _scan_path(generation_dir) is _PathScanOutcome.ABSENT
        ):
            with contextlib.suppress(OSError):
                _unlink_active_lock_if_same(path, identity)


def powershell_wrapper_log_sequence_policy() -> str:
    """Render the canonical uncertainty table for the generated launcher."""
    entries = "; ".join(
        f"'{state}' = ${str(uncertain).lower()}"
        for state, uncertain in _SEQUENCE_UNCERTAINTY_BY_STATE.items()
    )
    return "@{ " + entries + " }"


def _read_wrapper_log_sequence(
    generation: Path,
    *,
    committed: bool,
) -> tuple[int | None, bool]:
    """Read one launch sequence under the shared Python/PowerShell policy.

    ``.sequence-uncertain`` records that THIS generation's own sequence
    number may already be lower than the true prior maximum, because the
    allocator that wrote it could not fully scan every root (see
    ``_owned_committed_generations``). That fact belongs to the generation
    itself, permanently, independent of whether ``.sequence`` later reads
    back as perfectly well-formed - a later launch seeing a syntactically
    valid ``.sequence`` file must not conclude the uncertainty it was
    marked with has gone away. Without this, the marker is written but
    never consulted: retention on a later launch would rank this
    generation as confidently ordered, exactly the silent-uncertainty
    pattern #113 raised the marker to prevent in the first place.
    """
    # Only a CONFIRMED absence of `.sequence-uncertain` clears this - a
    # present OR unusable (unreadable) marker both mean this generation
    # cannot be trusted as confidently ordered (#113 review, round 3: the
    # prior `.exists()` form swallowed an unreadable marker into a
    # confident "no marker", indistinguishable from a generation that was
    # never marked uncertain at all).
    marker_uncertain = committed and (
        _scan_marker(generation / ".sequence-uncertain") is not _MarkerScanOutcome.ABSENT
    )
    sequence_path = generation / ".sequence"
    try:
        sequence_info = sequence_path.lstat()
    except FileNotFoundError:
        # Cannot confirm `.sequence-failed` is genuinely absent -> cannot
        # confirm this is a legacy generation that predates the marker
        # system, so fail toward the state that reports uncertain (#113
        # review, round 3).
        state = (
            "missing-legacy"
            if _scan_marker(generation / ".sequence-failed") is _MarkerScanOutcome.ABSENT
            else "missing-write-failed"
        )
        return None, bool(
            marker_uncertain or (committed and _SEQUENCE_UNCERTAINTY_BY_STATE[state])
        )
    except OSError:
        state = "present-invalid"
        return None, bool(
            marker_uncertain or (committed and _SEQUENCE_UNCERTAINTY_BY_STATE[state])
        )

    state = "present-invalid"
    sequence: int | None = None
    try:
        if (
            not stat.S_ISREG(sequence_info.st_mode)
            or _is_reparse_or_symlink(sequence_path)
        ):
            raise ValueError("sequence record is not a plain file")
        text = sequence_path.read_text(encoding="utf-8").strip()
        if not _SEQUENCE_RECORD_RE.fullmatch(text):
            raise ValueError("sequence record is not a canonical positive integer")
        parsed = int(text)
        if parsed > _SEQUENCE_MAX:
            raise ValueError("sequence record exceeds signed 64-bit range")
        sequence = parsed
        state = "present-valid"
    except (OSError, UnicodeError, ValueError):
        pass
    return sequence, bool(
        marker_uncertain or (committed and _SEQUENCE_UNCERTAINTY_BY_STATE[state])
    )


class _PathScanOutcome(enum.Enum):
    """The three CLOSED outcomes this scan is allowed to distinguish - no
    boolean or null channel is permitted to carry this distinction,
    because a boolean cannot represent "I could not tell" (#113 review,
    reviewer-1's sharpened class fix, replacing an earlier bool-pair
    predicate that still let an impossible fourth combination typecheck).

    ABSENT: genuinely not there.
    PLAIN_DIRECTORY: confirmed a real directory, safe to use.
    UNUSABLE: neither of the above could be positively confirmed.
    """

    ABSENT = "absent"
    PLAIN_DIRECTORY = "plain_directory"
    UNUSABLE = "unusable"


def _scan_path(path: Path) -> _PathScanOutcome:
    """Classify `path` for this scan via the raw, non-swallowing lstat()
    primitive into exactly one of the three outcomes above.

    ``Path.exists`` / ``.is_dir`` (called) are boolean wrappers that
    SWALLOW OSError into a confident False - only some error codes on
    3.10 (e.g. a disconnected/not-ready volume), but ANY OSError on
    3.14+ (``Path.exists`` there delegates to ``os.path.exists``, whose
    own implementation is a bare ``except (OSError, ValueError)``). A
    temporarily unreadable entry would then read exactly like one that
    never existed - a confident "no" instead of "unknown" - and this
    scan's whole caller-visible contract is that ambiguity becomes
    ``UNUSABLE``, never silently ``ABSENT`` (#113 review). ``lstat()`` is
    the raw primitive: only ``FileNotFoundError`` means genuinely absent;
    anything else (permission denied, a disconnected volume, an entry
    that exists but is not a directory, or a reparse/symlink component
    anywhere in its ancestry) is ``UNUSABLE`` - "could not positively
    confirm a plain directory," which this scan is required to treat the
    same way everywhere in it.

    Every level of this scan (the agent directory, each generation inside
    it, and any level added later) is required to go through this ONE
    classifier rather than hand-roll its own ``is_dir()``/``exists()``
    check - there is no other channel through which to ask the question,
    so a new level cannot reintroduce this class independently (#113
    review; confirmed a third instance of the same bug at a second level
    of the same scan before this classifier existed).
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _PathScanOutcome.ABSENT
    except OSError:
        return _PathScanOutcome.UNUSABLE
    if not stat.S_ISDIR(info.st_mode):
        return _PathScanOutcome.UNUSABLE
    try:
        if _has_reparse_or_symlink_component(path):
            return _PathScanOutcome.UNUSABLE
    except OSError:
        return _PathScanOutcome.UNUSABLE
    return _PathScanOutcome.PLAIN_DIRECTORY


class _MarkerScanOutcome(enum.Enum):
    """The closed outcomes for a marker FILE probe - the same discipline
    as ``_PathScanOutcome``, extended honestly for files rather than
    reusing the directory-shaped enum (a marker file needs no
    reparse/symlink-ancestry validation the way a retention-critical
    directory does, so a distinct, narrower outcome type is the honest
    fit, not a bolted-on boolean at the file level - #113 review, round
    3). ``ABSENT`` only for ``FileNotFoundError``; ``UNUSABLE`` for
    anything else this cannot positively confirm either way, INCLUDING a
    successful stat of something that is not a plain file (#113 review,
    round 4: a closed outcome set with an under-specified member is not
    closed - ``PRESENT`` must mean a valid marker leaf, not merely that
    some filesystem object occupies the name; a directory placed where
    `.committed` is expected read as PRESENT before this check existed).
    A marker that cannot be read is not a marker that is not there.
    """

    ABSENT = "absent"
    PRESENT = "present"
    UNUSABLE = "unusable"


def _scan_marker(path: Path) -> _MarkerScanOutcome:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _MarkerScanOutcome.ABSENT
    except OSError:
        return _MarkerScanOutcome.UNUSABLE
    if not stat.S_ISREG(info.st_mode):
        # Also covers a symlink/reparse point AT the leaf itself: lstat()
        # does not follow it, so its mode is S_ISLNK, never S_ISREG - no
        # separate symlink check is needed here the way the directory
        # classifier needs one for its ancestry.
        return _MarkerScanOutcome.UNUSABLE
    return _MarkerScanOutcome.PRESENT


def _owned_committed_generations(
    roots: tuple[Path, ...],
    agent_leaf: str,
) -> tuple[list[tuple[str, Path]], int, int, bool]:
    """Return (candidates, observed_count, max_sequence, uncertain).

    ``candidates`` is the deletion-eligible pool - positively-committed
    generations only, exactly as before. ``observed_count`` is a
    SEPARATE, wider count: every generation-shaped directory this scan
    actually found, whether or not its commit status could be
    confirmed. These two numbers must not be conflated (#113 review,
    round 5, finding #7): the safety bound that decides whether pruning
    is even attempted this cycle must be checked against how many
    generations physically exist, not against how many happened to
    qualify for deletion. Round 4's fix correctly excluded an ambiguous
    generation from ``candidates`` (right, for deletion), but the caller
    then also used ``len(candidates)`` for the bound - so the ambiguous
    generation vanished from BOTH numbers, undercounting the bound the
    same way undercounting a sequence value was already known to be
    dangerous. Executed and confirmed: 13 physical generations against a
    bound of 12, one genuinely ambiguous, survived by luck (12 <= 12)
    rather than by the bound correctly seeing 13.
    """
    candidates: list[tuple[str, Path]] = []
    observed_count = 0
    max_sequence = 0
    uncertain = False
    for root in roots:
        agent_dir = root / agent_leaf
        try:
            agent_outcome = _scan_path(agent_dir)
            if agent_outcome is not _PathScanOutcome.PLAIN_DIRECTORY:
                uncertain = uncertain or agent_outcome is _PathScanOutcome.UNUSABLE
                continue
            for generation in agent_dir.iterdir():
                if not _GENERATION_NAME_RE.fullmatch(generation.name):
                    continue
                generation_outcome = _scan_path(generation)
                if generation_outcome is not _PathScanOutcome.PLAIN_DIRECTORY:
                    if generation_outcome is _PathScanOutcome.UNUSABLE:
                        # Detected SOMETHING generation-shaped that could
                        # not be fully confirmed - still counts toward
                        # the physical population the bound must see,
                        # even though it can never be a deletion
                        # candidate. A genuinely ABSENT result (a
                        # same-cycle TOCTOU vanish between iterdir() and
                        # this probe) does not - it truly is not there.
                        observed_count += 1
                        uncertain = True
                    continue
                observed_count += 1
                committed_marker = _scan_marker(generation / ".committed")
                if committed_marker is _MarkerScanOutcome.UNUSABLE:
                    # Cannot confirm committed one way or the other.
                    # Counting this generation's ambiguity toward the
                    # safety bound is conservative and fine; resolving the
                    # ambiguity AS committed is not - that makes an
                    # unconfirmed, possibly still-pending generation
                    # DELETION-ELIGIBLE the moment the bound is crossed.
                    # Deletion needs positive proof of commitment, never a
                    # permissive resolution of an unknown (#113 review,
                    # round 4: reviewer-1's reproduction did exactly this
                    # - 13 generations, the oldest genuinely pending with
                    # captured stdout, only its .committed probe
                    # unreadable, and prune deleted it once the bound was
                    # crossed). Treat it the same as genuinely not
                    # committed - excluded from `candidates`, never a
                    # prune candidate - but still flag the whole scan
                    # uncertain (it was already counted in
                    # observed_count above, regardless of commit status).
                    uncertain = True
                committed = committed_marker is _MarkerScanOutcome.PRESENT
                sequence, sequence_uncertain = _read_wrapper_log_sequence(
                    generation,
                    committed=committed,
                )
                uncertain = uncertain or sequence_uncertain
                if sequence is not None:
                    max_sequence = max(max_sequence, sequence)
                if not committed:
                    continue
                sort_key = (
                    f"1-{sequence:020d}-{generation.name}"
                    if sequence is not None
                    else f"0-{generation.name}"
                )
                candidates.append((sort_key, generation))
        except OSError:
            uncertain = True
    return candidates, observed_count, max_sequence, uncertain


def _write_restrictive_text(path: Path, text: str) -> None:
    opener = _restrictive_file_opener if os.name != "nt" else None
    with open(str(path), "x", encoding="utf-8", opener=opener) as stream:
        stream.write(text)


class WrapperLogAllocationFailed(OSError):
    """Every candidate wrapper-log root was rejected; carries why each was.

    ``_allocate_wrapper_log_targets`` previously returned ``None`` on total
    failure, discarding every candidate's own ``OSError`` along the way. That
    collapsed "could not allocate anywhere, and here is why" into a bare
    "did not allocate" - the caller (and anyone reading logs afterward) had
    no way to distinguish a transient permission problem from, say, a path
    long enough to exceed Windows' legacy MAX_PATH. This carries the reason
    for every attempt so total failure is diagnosable instead of silent.
    """

    def __init__(self, attempts: tuple[tuple[Path, str], ...]) -> None:
        self.attempts = attempts
        if attempts:
            detail = "; ".join(f"{root}: {reason}" for root, reason in attempts)
        else:
            detail = "no candidate wrapper log root was available"
        super().__init__(f"no writable wrapper log root ({detail})")


def _allocate_wrapper_log_targets(
    project_root: Path,
    agent: str,
    environ: Mapping[str, str],
) -> _AllocatedWrapperLogTargets:
    roots = wrapper_log_root_candidates(project_root, environ=environ)
    agent_leaf = _wrapper_log_agent_leaf(agent)
    _candidates, _observed_count, max_sequence, uncertain = _owned_committed_generations(
        roots,
        agent_leaf,
    )
    sequence = max_sequence + 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    attempts: list[tuple[Path, str]] = []
    for root in roots:
        generation_dir: Path | None = None
        try:
            agent_dir = _prepare_agent_log_dir(root, agent_leaf)
            generation_dir = agent_dir / f"{stamp}-{secrets.token_hex(16)}"
            generation_dir.mkdir(mode=0o700)
            # Routed through the shared classifier (#113 review, round 5
            # sweep) - same reasoning as _prepare_agent_log_dir's
            # post-creation checks: behavior is unchanged, only the raise
            # site's message text differs.
            if _scan_path(generation_dir) is not _PathScanOutcome.PLAIN_DIRECTORY:
                raise OSError(
                    f"unsafe wrapper log generation directory: {generation_dir}"
                )
            stdout_path = generation_dir / "stdout.log"
            stderr_path = generation_dir / "stderr.log"
            _write_restrictive_text(stdout_path, "")
            _write_restrictive_text(stderr_path, "")
            _write_restrictive_text(generation_dir / ".pending", "")
            try:
                _write_restrictive_text(
                    generation_dir / ".sequence",
                    str(sequence),
                )
            except OSError:
                uncertain = True
                with contextlib.suppress(OSError):
                    _write_restrictive_text(
                        generation_dir / ".sequence-failed",
                        "",
                    )
            if uncertain:
                with contextlib.suppress(OSError):
                    _write_restrictive_text(
                        generation_dir / ".sequence-uncertain",
                        "",
                    )
            _harden_posix_log_paths(stdout_path, stderr_path)
            # Match the generated launcher's evidence-first ordering: this
            # pending generation proves a destination accepted creation, so it
            # is safe to trim only *prior* committed evidence now. It is not
            # itself eligible until the wrapper holds its lifetime lock and
            # confirms after both tees are live.
            _prune_wrapper_log_generations(
                roots,
                agent_leaf,
                sequence_uncertain=uncertain,
            )
            return _AllocatedWrapperLogTargets(
                root=root.resolve(),
                generation_dir=generation_dir.resolve(),
                stdout_path=stdout_path.resolve(),
                stderr_path=stderr_path.resolve(),
                roots=roots,
                agent_leaf=agent_leaf,
                sequence_uncertain=uncertain,
                rejected_attempts=tuple(attempts),
            )
        except OSError as exc:
            attempts.append((root, str(exc)))
            # The ancestry probe itself can raise OSError (the candidate
            # became inaccessible or disconnected after its generation
            # directory was created but before allocation finished). That
            # must not escape this handler: an unsuppressed second error
            # here would abort the whole candidate loop and disable capture
            # entirely, rather than just this one candidate (#113 review).
            # An uncheckable partial directory is unsafe to delete, so
            # leave it in place and still continue to the next candidate.
            if generation_dir is not None:
                with contextlib.suppress(OSError):
                    if not _has_reparse_or_symlink_component(generation_dir):
                        shutil.rmtree(generation_dir)
    raise WrapperLogAllocationFailed(tuple(attempts))


def _prune_wrapper_log_generations(
    roots: tuple[Path, ...],
    agent_leaf: str,
    *,
    sequence_uncertain: bool = False,
) -> None:
    candidates, observed_count, _max_sequence, uncertain = _owned_committed_generations(
        roots,
        agent_leaf,
    )
    safety_bound = max(
        WRAPPER_LOG_GENERATIONS * 3,
        WRAPPER_LOG_GENERATIONS + 2,
    )
    uncertain = uncertain or sequence_uncertain
    # The bound must be checked against how many generations PHYSICALLY
    # exist (observed_count), not how many happen to qualify for
    # deletion (len(candidates)) - #113 review, round 5, finding #7.
    # These can differ: an ambiguous generation is correctly excluded
    # from candidates (never deletion-eligible) but must still count
    # toward the bound, or the bound silently shrinks by exactly the
    # generations it exists to protect.
    if uncertain and observed_count <= safety_bound:
        return
    for _sort_key, generation in sorted(candidates)[:-WRAPPER_LOG_GENERATIONS]:
        if _has_reparse_or_symlink_component(generation):
            continue
        with _guard_wrapper_log_prune(generation) as prunable:
            if not prunable:
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(generation)


def _wrapper_log_location_path(state_dir: Path, agent: str) -> Path:
    from agenttalk.store import validate_agent_name

    safe_agent = validate_agent_name(agent)
    return (
        state_dir
        / "wrapper-log-locations"
        / f"{_wrapper_log_agent_leaf(safe_agent)}.json"
    )


def _record_wrapper_log_location(
    project_root: Path,
    agent: str,
    installation: WrapperLogInstallation,
) -> None:
    if not installation.confirmed or installation.generation_dir is None:
        return
    store_dir = project_root / ".agenttalk"
    # Routed through the shared classifier (#113 review, round 5 sweep) -
    # a real behavior change: only a CONFIRMED absence of config.json
    # should skip writing the location record. The prior .is_file()
    # swallowed an unreadable-but-present config.json into "not a
    # store," silently losing forensic capability for this launch; an
    # actual write failure is still safely handled by the except clause
    # below.
    if _scan_marker(store_dir / "config.json") is _MarkerScanOutcome.ABSENT:
        return
    try:
        path = _wrapper_log_location_path(store_dir / "state", agent)
        payload = {
            "schema_version": _LOCATION_SCHEMA_VERSION,
            "agent": agent,
            "root": str(installation.root),
            "generation_dir": str(installation.generation_dir),
            "stdout": str(installation.stdout_path),
            "stderr": str(installation.stderr_path),
            "wrapper_pid": os.getpid(),
            "observed_at": _utc_iso(datetime.now(timezone.utc).timestamp()),
        }
        _atomic.write_text(
            path,
            json.dumps(payload, indent=2, sort_keys=True),
        )
    except (OSError, ValueError):
        pass


def record_wrapper_log_location(
    project_root: Path,
    agent: str,
    installation: WrapperLogInstallation,
) -> None:
    """Record the accepted destination after authoritative root discovery.

    Capture can begin from a provisional, non-fallible root hint before the
    wrapper performs project discovery.  Once discovery succeeds, publish the
    already-accepted generation under the authoritative project's state
    directory without making diagnostics a new failure mode.
    """
    try:
        diagnostic_project = _diagnostic_project_root(project_root)
    except (OSError, RuntimeError):
        return
    _record_wrapper_log_location(diagnostic_project, agent, installation)


def read_wrapper_log_location(state_dir: Path, agent: str) -> dict[str, object]:
    """Read the last wrapper-recorded location without recomputing candidates."""
    absent = {
        "status": "absent",
        "root": None,
        "generation_dir": None,
        "stdout": None,
        "stderr": None,
    }
    try:
        path = _wrapper_log_location_path(state_dir, agent)
    except ValueError:
        return {**absent, "status": "invalid", "error": "unsafe_agent"}
    # #113 review, round 5: _scan_marker deliberately has no ancestry
    # check (reviewer-1's own guidance in round 4 - a generation-relative
    # marker lives inside a generation directory whose ancestry the
    # RETENTION scan already validates via _scan_path, so re-validating
    # per marker would be redundant there). This location record does
    # NOT live inside a pre-validated generation directory - it lives
    # under state_dir, which nothing else in this read path validates.
    # A junctioned ancestor anywhere under state_dir would otherwise let
    # this function read and confidently report content from wherever
    # that redirect actually points. Validated explicitly, once, here -
    # not folded into _scan_marker itself, since the marker's OTHER
    # callers correctly do not need it.
    try:
        if _has_reparse_or_symlink_component(path):
            return {**absent, "status": "unusable"}
    except OSError:
        return {**absent, "status": "unusable"}
    location_marker = _scan_marker(path)
    if location_marker is _MarkerScanOutcome.ABSENT:
        return absent
    if location_marker is _MarkerScanOutcome.UNUSABLE:
        # Cannot confirm the record is genuinely absent, so this must not
        # read the same as one - a caller falling back on "absent" would
        # incorrectly refuse a lookup a readable filesystem would have
        # answered (#113 review, round 3).
        return {**absent, "status": "unusable"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("location is not an object")
        if payload.get("schema_version") != _LOCATION_SCHEMA_VERSION:
            raise ValueError("unsupported schema")
        if payload.get("agent") != agent:
            raise ValueError("agent mismatch")
        paths = {
            name: Path(str(payload[name])).resolve()
            for name in ("root", "generation_dir", "stdout", "stderr")
            if isinstance(payload.get(name), str) and payload[name]
        }
        if set(paths) != {"root", "generation_dir", "stdout", "stderr"}:
            raise ValueError("missing path")
        generation = paths["generation_dir"]
        if generation.parent.name != _wrapper_log_agent_leaf(agent):
            raise ValueError("agent directory mismatch")
        if generation.parent.parent != paths["root"]:
            raise ValueError("root mismatch")
        if paths["stdout"] != generation / "stdout.log":
            raise ValueError("stdout mismatch")
        if paths["stderr"] != generation / "stderr.log":
            raise ValueError("stderr mismatch")
        # Found during the round-3 sweep, not itself cited: the same
        # exists()-family swallow one level below the top-level check
        # above. A generation/stdout/stderr this cannot read is not
        # confirmed gone (stale) any more than it is confirmed present
        # (observed) - report "unusable" rather than picking either
        # confident answer from an unreadable probe.
        #
        # Round 4 correction: these three expect DIFFERENT object kinds -
        # generation must be a directory, stdout/stderr must be plain
        # files - so this must classify each through the type-aware
        # classifier for its own kind, not the presence-only marker
        # probe. A presence-only check would report "observed" for a
        # directory sitting where stdout.log belongs, exactly the
        # under-specified-PRESENT defect just fixed in _scan_marker
        # itself (#113 review, round 4).
        generation_outcome = _scan_path(generation)
        stdout_outcome = _scan_marker(paths["stdout"])
        stderr_outcome = _scan_marker(paths["stderr"])
        if (
            generation_outcome is _PathScanOutcome.PLAIN_DIRECTORY
            and stdout_outcome is _MarkerScanOutcome.PRESENT
            and stderr_outcome is _MarkerScanOutcome.PRESENT
        ):
            status = "observed"
        elif (
            generation_outcome is _PathScanOutcome.UNUSABLE
            or stdout_outcome is _MarkerScanOutcome.UNUSABLE
            or stderr_outcome is _MarkerScanOutcome.UNUSABLE
        ):
            status = "unusable"
        else:
            status = "stale"
        return {
            "status": status,
            "root": str(paths["root"]),
            "generation_dir": str(generation),
            "stdout": str(paths["stdout"]),
            "stderr": str(paths["stderr"]),
            "wrapper_pid": payload.get("wrapper_pid"),
            "observed_at": payload.get("observed_at"),
        }
    except OSError as exc:
        # #113 review, round 5, finding #13: a read denial or a
        # path-resolution failure occurring past the top-level probe
        # (e.g. a race on read_text(), or an unresolvable ancestor in
        # one of the stored paths) means "could not read/resolve," not
        # "content is malformed" - the same distinction the docs already
        # promise for the top-level check. Only the FIRST probe was
        # classifying OSError this way; this closes the gap so the
        # public contract holds end to end, not just at the first site.
        return {
            **absent,
            "status": "unusable",
            "error": type(exc).__name__,
        }
    except (
        KeyError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return {
            **absent,
            "status": "invalid",
            "error": type(exc).__name__,
        }
# endregion wrapper-log-retention


class BoundedStreamTee:
    """Text stream that bounds the inherited redirect and keeps a newest-output ring.

    ``base_path`` is the file PowerShell supplied to
    ``-RedirectStandardOutput``/``-RedirectStandardError``.  The original stream
    continues feeding that file only for one segment.  Suffix segments
    ``.1`` ... ``.<N-1>`` are owned directly by this process and cycle in place.
    Thus every file is bounded and the complete generation stays under
    ``max_bytes`` during normal operation.

    If the suffix ring becomes unwritable, the wrapper continues and the
    original stream remains capped; excess diagnostic output is discarded
    rather than turning a logging failure into either a launch failure or an
    unbounded disk write.
    """

    def __init__(
        self,
        original: TextIO,
        base_path: str | os.PathLike[str],
        *,
        max_bytes: int = WRAPPER_LOG_MAX_BYTES,
        segment_count: int = WRAPPER_LOG_SEGMENT_COUNT,
        resume: bool = False,
        mirror: TextIO | None = None,
    ) -> None:
        if max_bytes < _MIN_MAX_BYTES or max_bytes > _MAX_MAX_BYTES:
            raise ValueError("max_bytes is outside the supported bound")
        if segment_count < _MIN_SEGMENTS or segment_count > _MAX_SEGMENTS:
            raise ValueError("segment_count is outside the supported bound")
        self._original = original
        self._mirror = mirror
        self.base_path = Path(base_path)
        self.max_bytes = int(max_bytes)
        self.segment_count = int(segment_count)
        self.segment_bytes = max(1, self.max_bytes // self.segment_count)
        with contextlib.suppress(OSError, ValueError):
            self._original.flush()
        try:
            existing_base_bytes = self.base_path.stat().st_size
        except OSError:
            existing_base_bytes = 0
        self._forward_remaining = max(
            0,
            self.segment_bytes - existing_base_bytes,
        )
        self._tail_count = self.segment_count - 1
        self._tail_index = 0
        self._tail_size = 0
        # THE invariant that closes the whole class of "this ring lost the
        # newest bytes" findings (round 18's trade-off, round 21/29's mtime
        # ambiguity, round 29's missing-cursor fallback, and round 29's
        # cursor-persistence-failure cascade - five different mechanisms,
        # one recurring outcome): _open_tail may open a suffix with "wb"
        # ONLY if THIS instance has itself already opened that exact index
        # before, in memory, this run. A suffix this instance has never
        # visited is NEVER truncated, no matter how _tail_index ended up
        # pointing at it - a correct cursor, a stale cursor, no cursor at
        # all, or a mid-write cascade through _advance_tail landing on an
        # index nothing here has picked deliberately. Every one of those
        # paths converges on the same safe default in _open_tail. Once a
        # full ring lap has genuinely passed and this instance revisits an
        # index it already knows about, "wb" is exactly the correct,
        # expected ring behavior - discarding the oldest tracked content,
        # not a guess about untracked content.
        self._visited_tail_indices: set[int] = set()
        if resume:
            self._resume_tail_position()
        self._tail = None
        self._tail_failed = False
        self._closed = False
        self._lock = threading.Lock()

    def _tail_cursor_path(self) -> Path:
        # semgrep's agenttalk-no-raw-agent-name-in-filename fires on the
        # SYNTACTIC shape f"...{$NAME}.cursor" regardless of what $NAME
        # actually holds - it exists to catch an UNVALIDATED AGENT NAME
        # reaching a path (the v0.2.1 path-traversal class; see
        # SECURITY.md). self.base_path.name is never an agent name: it is
        # this class's own fixed base_path (always literally "stdout.log"
        # or "stderr.log", set once in __init__ from the supervisor's own
        # ENV_STDOUT_PATH/ENV_STDERR_PATH, never operator- or
        # agent-name-derived text) - the agent-name-to-path validation
        # this rule protects already happened one layer up, in
        # Get-SafeWrapperLogAgentDir's own hashing of the agent name into
        # the DIRECTORY component, before this filename is ever built.
        return self.base_path.with_name(
            f"{self.base_path.name}.cursor"  # nosemgrep: agenttalk-no-raw-agent-name-in-filename
        )

    def _write_tail_cursor(self) -> None:
        # Best-effort, same as the rest of this ring: a failure here must
        # not crash a wrapper that is otherwise fine - it only leaves a
        # LATER resumed instance unable to find a recorded cursor, which
        # _resume_tail_position already treats as "start fresh" rather
        # than truncating anything (see its own comment).
        opener = _restrictive_file_opener if os.name != "nt" else None
        try:
            with open(str(self._tail_cursor_path()), "w", opener=opener) as f:
                f.write(str(self._tail_index))
        except OSError:
            pass

    def _resume_tail_position(self) -> None:
        # PR 98 round 29 connector finding: a fresh instance re-constructed
        # against a base_path a PRIOR instance already wrote into
        # (print_bounded_uncaught_exception's second, top-level tee is the
        # one real caller of this) has no memory of where that prior
        # instance left off. Defaulting to _tail_index=0 and opening .1
        # with "wb" would TRUNCATE whichever segment happens to sit there -
        # which, after a normal rotation cycle, is as likely to be the
        # NEWEST content as the oldest.
        #
        # This USED to probe the filesystem for the suffix with the newest
        # st_mtime - a fragile INFERENCE, not a recorded fact: on a
        # filesystem with coarse timestamp resolution, or across a
        # backward clock adjustment, that inference can name the WRONG
        # suffix as current even though a different one holds the actual
        # newest output. If that wrongly-chosen suffix is already near
        # full, the very next write advances past it and opens the
        # following suffix with "wb" - truncating the real newest lifecycle
        # and crash evidence this whole module exists to preserve. Sharper
        # tie-breaking narrows the odds without closing the class - the
        # same lesson this project already learned once at a larger scale
        # (supervisor liveness moved from PID/process-tree inference to
        # heartbeat staleness for exactly this reason).
        #
        # Fixed by RECORDING the fact instead of inferring it:
        # _write_tail_cursor persists which suffix is current every time
        # _open_tail actually opens one (first activation and every
        # rotation), and this reads that recorded index back - no mtime,
        # no tie-break, nothing wall-clock-dependent.
        #
        # This only has to pick a STARTING index - it does not have to
        # get it right, and it does not set up "append the first open,
        # then trust the rest" bookkeeping the way earlier drafts did.
        # _open_tail's own per-index visited-set check (see __init__'s
        # comment) is what actually guarantees nothing gets truncated on
        # a first visit, for THIS index and for any index a later
        # rotation cascades into - a missing/unreadable cursor (no prior
        # instance ever wrote one, or it predates this fix), a STALE
        # cursor (the prior instance's cursor REWRITE failed after it had
        # already advanced - round 29 rereview), or a correct cursor all
        # land on the exact same safe path below, uniformly, rather than
        # each needing its own special case here.
        index = 0
        try:
            raw = self._tail_cursor_path().read_text(encoding="utf-8").strip()
            parsed = int(raw)
            if 0 <= parsed < self._tail_count:
                index = parsed
        except (OSError, ValueError):
            pass
        self._tail_index = index

    @property
    def encoding(self) -> str:
        source = self._mirror if self._mirror is not None else self._original
        return getattr(source, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        source = self._mirror if self._mirror is not None else self._original
        return getattr(source, "errors", None) or "replace"

    @property
    def closed(self) -> bool:
        return self._closed

    def isatty(self) -> bool:
        if self._mirror is None:
            return False
        return bool(self._mirror.isatty())

    def fileno(self) -> int:
        source = self._mirror if self._mirror is not None else self._original
        return source.fileno()

    def writable(self) -> bool:
        return not self._closed

    def _tail_path(self) -> Path:
        return self.base_path.with_name(
            f"{self.base_path.name}.{self._tail_index + 1}"
        )

    def _open_tail(self) -> None:
        # mode=0o700 is applied atomically by mkdir itself (harmless no-op
        # shape on Windows) - unlike a chmod issued after the fact, there is
        # no window where a freshly-created directory sits at the default,
        # umask-derived mode.
        self.base_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._tail_path()
        # A custom opener bakes the restrictive mode into the SAME os.open()
        # call that creates the file, closing the window a separate chmod
        # afterward cannot: another local user on a shared POSIX account
        # could open a handle to this file in that window and keep it past
        # the chmod. The mode argument only takes effect when the file is
        # actually being CREATED (O_CREAT) - harmless, and irrelevant, for
        # an "ab" open reopening a file that already exists.
        # pathlib.Path.open() does not forward an opener kwarg - use the
        # builtin open() (which does) against the string path instead.
        opener = _restrictive_file_opener if os.name != "nt" else None
        # THE invariant (see __init__'s comment): "wb" is only ever used on
        # an index THIS instance has already opened before, in memory -
        # never on the strength of a cursor, a guess, or anywhere a
        # cascading rotation happens to land. A first-ever visit to an
        # index always appends onto whatever is actually on disk right
        # now (seeded by a fresh stat, not by inherited bookkeeping from
        # whichever segment this instance was previously using) - a
        # nonexistent file opened "ab" is created fresh, identical to
        # "wb" in that specific case, so this costs nothing for the
        # ordinary, uninterrupted-instance path.
        if self._tail_index in self._visited_tail_indices:
            self._tail = open(str(path), "wb", opener=opener)
            self._tail_size = 0
        else:
            try:
                self._tail_size = path.stat().st_size
            except OSError:
                self._tail_size = 0
            self._tail = open(str(path), "ab", opener=opener)
        self._visited_tail_indices.add(self._tail_index)
        if os.name != "nt":
            # Defense in depth for a path this opener could not have
            # created fresh - e.g. a resumed file left behind by an older
            # agenttalk version that predates this fix.
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        # Record which suffix is now current AFTER it is actually open, not
        # before - a failed open here must never leave a cursor pointing at
        # a suffix this instance does not actually hold open.
        self._write_tail_cursor()

    def _advance_tail(self) -> None:
        if self._tail is not None:
            self._tail.flush()
            self._tail.close()
        self._tail_index = (self._tail_index + 1) % self._tail_count
        self._tail = None
        self._open_tail()

    def _write_tail(self, raw: bytes) -> None:
        remaining = memoryview(raw)
        while remaining:
            if self._tail is None:
                self._open_tail()
            if self._tail_size >= self.segment_bytes:
                self._advance_tail()
            available = self.segment_bytes - self._tail_size
            boundary = _utf8_chunk_boundary(remaining, available)
            if boundary == 0:
                # The next code point does not fit in what is left of this
                # segment at all - not only right after a rotation, but any
                # time a nearly-full segment's remaining room is narrower
                # than the code point landing there. Rotating to a fresh
                # segment (segment_bytes is bounded well above 4 bytes, the
                # max UTF-8 sequence length) gives it room to fit whole,
                # rather than splitting its encoded bytes across files, and
                # rather than looping forever re-deriving the same
                # zero-byte boundary against an unchanged buffer.
                self._advance_tail()
                available = self.segment_bytes
                boundary = _utf8_chunk_boundary(remaining, available)
                if boundary == 0:
                    # Unreachable given the enforced segment_bytes bound -
                    # a last resort so a future bound change fails soft
                    # (one split code point) rather than hanging.
                    boundary = 1
            chunk = remaining[:boundary]
            tail = self._tail
            if tail is None:
                raise OSError("wrapper log tail is unavailable")
            # Account for the write BEFORE issuing it, not after: a
            # terminating signal's handler runs between bytecode
            # instructions, so it can only land in the gap between this
            # call and the next one - never inside the write() call itself.
            # Accounting first means a signal landing in that gap leaves
            # self._tail_size OVERSTATED relative to the file (nothing was
            # actually written yet), which only rotates a little early on
            # the next write. Accounting after leaves it UNDERSTATED once
            # the bytes are already on disk, so the next write believes it
            # has more room than it does and can push the segment past
            # segment_bytes by up to another chunk.
            self._tail_size += len(chunk)
            tail.write(chunk)
            # I4 (cold review, round 3): once the base segment's forwarding
            # budget is spent, EVERY further diagnostic write lands only
            # here - the tail ring is the sole surviving copy at that
            # point, so it needs the same durability guarantee the base
            # segment's own flush-on-newline fix gives the original
            # stream. tail is a raw binary file (no line-buffering concept
            # to replicate); flush unconditionally rather than trying to
            # infer "was that a diagnostic line" from raw bytes.
            tail.flush()
            remaining = remaining[len(chunk):]

    def _write_original(self, text: str, *, bounded: bool) -> None:
        if bounded:
            prefix = _utf8_prefix(text, self._forward_remaining)
            if not prefix:
                return
            text = prefix
        encoded = text.encode("utf-8", "replace")
        if bounded:
            self._forward_remaining -= len(encoded)
        try:
            # Writing through self._original's own .write() would let its text
            # mode translate "\n" to the platform line separator (CRLF on
            # Windows) before it hits disk - the actual bytes written would
            # then exceed the pre-translation length just budgeted above,
            # letting a newline-heavy stream blow the cap by nearly 2x. Write
            # the already-UTF-8-encoded bytes straight to the underlying
            # buffer instead, so the accounted length is the written length.
            buffer = getattr(self._original, "buffer", None)
            if buffer is not None:
                buffer.write(encoded)
                # I4: bypassing .write() also bypasses line buffering's own
                # flush-on-newline - stderr is line-buffered by default, so
                # a diagnostic line written just before an uncatchable
                # SIGKILL would otherwise sit unflushed in the buffer's own
                # (larger, not newline-triggered) internal buffer and never
                # reach disk at all, defeating the reason this module
                # exists. Replicate exactly the condition TextIOWrapper
                # itself would have flushed on.
                if getattr(self._original, "line_buffering", False) and (
                    b"\n" in encoded
                ):
                    buffer.flush()
            else:
                self._original.write(text)
        except (OSError, ValueError):
            pass

    def write(self, value: object) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed wrapper log stream")
        text = str(value)
        raw = text.encode("utf-8", "replace")
        with self._lock:
            if not self._tail_failed:
                try:
                    self._write_tail(raw)
                except (OSError, ValueError):
                    self._tail_failed = True
                    if self._tail is not None:
                        with contextlib.suppress(OSError, ValueError):
                            self._tail.close()
                    self._tail = None
            self._write_original(text, bounded=True)
            if self._mirror is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._mirror.write(text)
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with self._lock:
            with contextlib.suppress(OSError, ValueError):
                self._original.flush()
            if self._mirror is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._mirror.flush()
            if self._tail is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._tail.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._tail is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._tail.flush()
                    self._tail.close()
            self._tail = None
            self._closed = True


# Set by installed_standard_streams_from_environment right before it installs
# the bounded tees, and deliberately never cleared: it is read exactly once,
# by print_bounded_uncaught_exception at the true top-level script boundary
# (agenttalk/__main__.py), which runs strictly after this module's own
# context manager has already restored sys.stdout/sys.stderr and is no
# longer reachable from that call site. Holding onto a resolved path and two
# bounded integers - not a stream or hook - so a stale value left over after
# a normal exit is inert rather than something that could leak behavior into
# unrelated later code.
_LAST_STDERR_LOG_CONFIG: tuple[str, int, int] | None = None
_LAST_ACTIVE_GENERATION_LOCK: _ActiveGenerationLock | None = None


@contextlib.contextmanager
def installed_standard_streams_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    expected_nonce: str | None = None,
    project_root: str | os.PathLike[str] | None = None,
    agent: str | None = None,
) -> Iterator[WrapperLogInstallation]:
    """Install wrapper-owned bounded capture, reusing authenticated targets.

    A generated supervisor can supply already-open redirect targets through its
    nonce-authenticated environment. Every other ``wrap`` launch allocates its
    own generation and mirrors the original console while writing the bounded
    files. Allocation and the location record are fail-soft diagnostics.
    """
    global _LAST_ACTIVE_GENERATION_LOCK, _LAST_STDERR_LOG_CONFIG
    env = os.environ if environ is None else environ
    authenticated = _authenticated_environment(env, expected_nonce)
    allocated: _AllocatedWrapperLogTargets | None = None
    diagnostic_project: Path | None = None
    owned_streams = contextlib.ExitStack()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    if project_root is not None:
        try:
            diagnostic_project = _diagnostic_project_root(project_root)
        except (OSError, RuntimeError):
            diagnostic_project = None
    if authenticated:
        stdout_raw = env.get(ENV_STDOUT_PATH)
        stderr_raw = env.get(ENV_STDERR_PATH)
        if not stdout_raw or not stderr_raw:
            yield WrapperLogInstallation(False)
            return
        stdout_path = Path(stdout_raw).resolve()
        stderr_path = Path(stderr_raw).resolve()
        stdout_base = original_stdout
        stderr_base = original_stderr
        stdout_mirror = None
        stderr_mirror = None
    elif diagnostic_project is not None and agent:
        allocation_failure_reason: str | None = None
        try:
            allocated = _allocate_wrapper_log_targets(
                diagnostic_project,
                str(agent),
                env,
            )
        except WrapperLogAllocationFailed as exc:
            allocated = None
            allocation_failure_reason = str(exc)
        except (OSError, RuntimeError) as exc:
            allocated = None
            allocation_failure_reason = str(exc)
        if allocated is None:
            yield WrapperLogInstallation(
                False,
                disabled_reason=allocation_failure_reason,
            )
            return
        stdout_path = allocated.stdout_path
        stderr_path = allocated.stderr_path
        opener = _restrictive_file_opener if os.name != "nt" else None
        try:
            stdout_base = owned_streams.enter_context(
                open(
                    str(stdout_path),
                    "a",
                    encoding="utf-8",
                    errors="replace",
                    buffering=1,
                    opener=opener,
                )
            )
            stderr_base = owned_streams.enter_context(
                open(
                    str(stderr_path),
                    "a",
                    encoding="utf-8",
                    errors="replace",
                    buffering=1,
                    opener=opener,
                )
            )
        except OSError:
            owned_streams.close()
            with contextlib.suppress(OSError):
                shutil.rmtree(allocated.generation_dir)
            yield WrapperLogInstallation(False)
            return
        stdout_mirror = original_stdout
        stderr_mirror = original_stderr
    else:
        yield WrapperLogInstallation(False)
        return

    _harden_posix_log_paths(stdout_path, stderr_path)
    max_bytes = _bounded_int(
        env.get(ENV_MAX_BYTES),
        default=WRAPPER_LOG_MAX_BYTES,
        minimum=_MIN_MAX_BYTES,
        maximum=_MAX_MAX_BYTES,
    )
    segment_count = _bounded_int(
        env.get(ENV_SEGMENT_COUNT),
        default=WRAPPER_LOG_SEGMENT_COUNT,
        minimum=_MIN_SEGMENTS,
        maximum=_MAX_SEGMENTS,
    )
    stdout = BoundedStreamTee(
        stdout_base,
        stdout_path,
        max_bytes=max_bytes,
        segment_count=segment_count,
        mirror=stdout_mirror,
    )
    stderr = BoundedStreamTee(
        stderr_base,
        stderr_path,
        max_bytes=max_bytes,
        segment_count=segment_count,
        mirror=stderr_mirror,
    )
    sys.stdout = stdout
    sys.stderr = stderr
    _LAST_STDERR_LOG_CONFIG = (str(stderr_path), max_bytes, segment_count)
    # Round 23: this is the ONE moment that proves - by evidence, not by a
    # pre-launch guess - that this launch actually reaches cmd_wrap and
    # installs its streams: authentication has already succeeded and both
    # tees are live. Confirm the generation directly from here, the same
    # way the supervisor used to (write .committed, drop .pending), so the
    # supervisor's commit decision is now the wrapper's own report instead
    # of a prediction about argv grammar, quoting, cwd, interpreter, or
    # timing it can never fully replicate.
    generation_dir = stderr_path.parent
    active_lock = _acquire_active_generation_lock(generation_dir)
    confirmed = bool(
        active_lock is not None
        and _confirm_wrapper_log_generation(generation_dir)
    )
    root = generation_dir.parent.parent
    installation = WrapperLogInstallation(
        True,
        confirmed=confirmed,
        root=root,
        generation_dir=generation_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        rejected_attempts=(
            allocated.rejected_attempts if allocated is not None else ()
        ),
        allocated_via_fallback_search=allocated is not None,
    )
    if diagnostic_project is not None and agent:
        _record_wrapper_log_location(
            diagnostic_project,
            str(agent),
            installation,
        )
    escaped = False
    try:
        yield installation
    except BaseException:
        escaped = True
        raise
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        stdout.close()
        stderr.close()
        owned_streams.close()
        if active_lock is not None:
            if escaped:
                # The real __main__ boundary appends one final bounded traceback
                # after this context unwinds. Keep deletion blocked until that
                # append completes; a caught exception merely leaves an OS-held
                # diagnostic lock until process exit, which is fail-safe.
                _LAST_ACTIVE_GENERATION_LOCK = active_lock
            else:
                with contextlib.suppress(OSError):
                    _release_active_generation_lock(active_lock)
                if allocated is not None and confirmed:
                    with contextlib.suppress(OSError):
                        _prune_wrapper_log_generations(
                            allocated.roots,
                            allocated.agent_leaf,
                            sequence_uncertain=allocated.sequence_uncertain,
                        )


def _confirm_wrapper_log_generation(generation_dir: Path) -> bool:
    """Mark a wrapper log generation committed from INSIDE the wrapper
    process itself, mirroring supervisor.py's own .pending/.committed
    marker pair (New-WrapperLogPendingMarker / the marker half of what was
    Complete-WrapperLogTargets) - best-effort: a failure here must not
    crash a wrapper that otherwise started fine, it just leaves the
    generation looking unresolved, which the supervisor's retention rule
    already treats as "preserve, never evict" rather than "safe to prune"."""
    try:
        (generation_dir / ".committed").write_bytes(b"")
        (generation_dir / ".pending").unlink(missing_ok=True)
        return True
    except OSError:
        return False


def print_bounded_uncaught_exception() -> None:
    """Print the currently-propagating exception the way Python's own
    top-level printer would - call only from the real script entry point
    (agenttalk/__main__.py), from an except block, once main() has let an
    exception escape uncaught.

    By the time control reaches here, installed_standard_streams_from_
    environment's own finally has already restored sys.stdout/sys.stderr -
    cmd_wrap's bare re-raise of an unexpected exception had to leave that
    restore unconditional so an embedder catching the same exception gets
    back clean, unmodified stream state. For a supervisor-launched wrapper,
    the restored stderr IS the raw file the supervisor redirected to, with
    no cap of its own - letting Python's default printer write a second,
    unbounded copy of a traceback there (the first copy already landed,
    correctly bounded, inside cmd_wrap before this ever runs) can push that
    file past the advertised cap. Route this copy through the same bounded
    tail-ring mechanism instead; a manual run with no wrapper log installed
    prints normally, unchanged.
    """
    global _LAST_ACTIVE_GENERATION_LOCK
    config = _LAST_STDERR_LOG_CONFIG
    try:
        if config is None:
            traceback.print_exc(file=sys.stderr)
            return
        stderr_path, max_bytes, segment_count = config
        try:
            tee = BoundedStreamTee(
                io.StringIO(),
                stderr_path,
                max_bytes=max_bytes,
                segment_count=segment_count,
                resume=True,
            )
            try:
                traceback.print_exc(file=tee)
            finally:
                tee.close()
        except Exception:
            # Best-effort: a failure bounding this redundant second copy must
            # not swallow the crash itself.
            with contextlib.suppress(Exception):
                traceback.print_exc(file=sys.stderr)
    finally:
        if _LAST_ACTIVE_GENERATION_LOCK is not None:
            with contextlib.suppress(OSError):
                _release_active_generation_lock(_LAST_ACTIVE_GENERATION_LOCK)
            _LAST_ACTIVE_GENERATION_LOCK = None


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class WrapperLifecycleLog:
    """Fail-soft JSONL of facts observed by one wrapper process."""

    def __init__(
        self,
        agent: str,
        *,
        stream: TextIO | None = None,
        clock=None,
        wrapper_pid: int | None = None,
        enabled: bool = True,
    ) -> None:
        import time

        self.agent = agent
        self.stream = sys.stderr if stream is None else stream
        self._clock = time.time if clock is None else clock
        self._wrapper_pid = os.getpid() if wrapper_pid is None else wrapper_pid
        self.enabled = bool(enabled)
        self._runtime: dict = {}
        self._pending_signal: tuple[int, bool] | None = None
        self.terminal_emitted = False

    @classmethod
    def from_environment(
        cls,
        agent: str,
        *,
        expected_nonce: str | None = None,
    ) -> "WrapperLifecycleLog":
        enabled = _authenticated_environment(os.environ, expected_nonce)
        return cls(agent, enabled=enabled)

    def _emit(self, event: str, **facts: object) -> None:
        if not self.enabled:
            return
        row: dict[str, object] = {
            "at": _utc_iso(float(self._clock())),
            "event": event,
            "agent": self.agent,
            "wrapper_pid": self._runtime.get(
                "wrapper_pid",
                self._wrapper_pid,
            ),
        }
        for field in _RUNTIME_FIELDS:
            row[field] = self._runtime.get(field)
        row.update(facts)
        try:
            self.stream.write(
                json.dumps(
                    row,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def runtime_transition(self, transition: str, record: dict) -> None:
        self._runtime = dict(record)
        event = _TRANSITION_EVENTS.get(transition)
        if event is not None:
            self._emit(event)

    def child_exited(
        self,
        child_pid: int,
        child_start: str | None,
        return_code: int,
    ) -> None:
        self._emit(
            "child_exited",
            child_pid=child_pid,
            child_start=child_start,
            return_code=return_code,
        )

    def wrapper_exited(self, exit_code: int, *, reason: str) -> None:
        self.terminal_emitted = True
        self._emit("wrapper_exited", exit_code=exit_code, reason=reason)

    def wrapper_exception(self, exc: BaseException) -> None:
        self.terminal_emitted = True
        self._emit("wrapper_exception", exception_type=type(exc).__name__)

    def signal_received(self, signum: int, *, terminating: bool = True) -> None:
        if terminating:
            self.terminal_emitted = True
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, TypeError):
            signal_name = None
        self._emit(
            "wrapper_signal_received",
            signal=int(signum),
            signal_name=signal_name,
            terminating=terminating,
        )

    def defer_signal(self, signum: int, *, terminating: bool) -> None:
        # A Python signal handler can interrupt a stream write between its file
        # write and accounting update. Record only the scalar here; emit after
        # the interrupted stack has unwound out of the signal context.
        self._pending_signal = (int(signum), bool(terminating))

    def flush_deferred_signal(self) -> None:
        pending = self._pending_signal
        self._pending_signal = None
        if pending is not None:
            signum, terminating = pending
            self.signal_received(signum, terminating=terminating)


@contextlib.contextmanager
def capture_termination_signals(
    lifecycle: WrapperLifecycleLog,
) -> Iterator[None]:
    """Log supported catchable signals, then preserve their prior behavior."""
    if not lifecycle.enabled or threading.current_thread() is not threading.main_thread():
        yield
        return
    supported = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None and value not in supported:
            supported.append(value)
    previous: dict[int, object] = {}

    def _handler(signum: int, frame: object) -> None:
        prior = previous.get(signum, signal.SIG_DFL)
        if callable(prior):
            try:
                prior(signum, frame)
            except BaseException:
                lifecycle.defer_signal(signum, terminating=True)
                raise
            lifecycle.defer_signal(signum, terminating=False)
            return
        if prior == signal.SIG_IGN:
            return
        lifecycle.defer_signal(signum, terminating=True)
        if signum == getattr(signal, "SIGINT", None):
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))

    try:
        for signum in supported:
            try:
                previous[int(signum)] = signal.getsignal(signum)
                signal.signal(signum, _handler)
            except (OSError, ValueError):
                previous.pop(int(signum), None)
        yield
    finally:
        for signum, prior in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signum, prior)
        lifecycle.flush_deferred_signal()
