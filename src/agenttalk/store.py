"""On-disk message store.

Layout under <root>/.agenttalk/:
    config.json            session config + agent roster
    messages/<id>.json     one file per message, lexicographically sorted by id
    state/<agent>.cursor   last message id this agent has acknowledged
    sessions/              exported transcripts

Message id format: ``YYYYMMDD-HHMMSS-uuuuuu-XXXX`` where the suffix is a
4-char random tag to avoid collisions when two messages land in the same
microsecond from different processes (the two agents). Within one process
the timestamp portion is forced monotonic by ``_new_id`` (see the function
docstring) so lexicographic order equals send order for any one writer —
the invariant ``messages_for`` / dashboard rendering relies on.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import string
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenttalk import health as _health
from agenttalk import gates as _gates
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk import avatars as _avatars
from agenttalk.redaction import normalize_child_output_tail
from agenttalk import signing as _signing

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

DIRNAME = ".agenttalk"

# Capability tokens the RUNNING store code supports. Surfaced by `doctor` so two
# writers reporting the same --version (e.g. a PYTHONPATH=src build and an
# installed wheel) are still discriminable — a writer lacking a token here is one
# that can leave the store in a state a token-bearing writer must repair (#37).
STORE_SCHEMA_CAPABILITIES = ("message-publication-order/v1",)

_ID_ALPHABET = string.ascii_letters + string.digits
# Canonical generated-message-id shape, built FROM _ID_ALPHABET so the
# validator can never drift from `_new_id` (which emits
# "%Y%m%d-%H%M%S-%f" + "-" + 4 chars of _ID_ALPHABET). A file whose id
# does not match this is classified invalid at scan time — it can never
# deliver or advance a cursor (0.18.0; closes the malformed-id
# cursor-poison). NOTE: this rejects wrong-SHAPE ids only; a well-formed
# but future-dated id from cross-machine clock skew still matches and is
# a documented constraint, not fixed here.
_ID_RE = re.compile(r"\A\d{8}-\d{6}-\d{6}-[" + re.escape(_ID_ALPHABET) + r"]{4}\Z")


def _safe_int(value: object) -> int:
    """Coerce a stored counter to int, degrading to 0 on null/non-numeric (a hand-edited
    or forward-incompatible ledger VALUE must err LOW, never raise - mirrors the
    degrade-to-empty read)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_LOCK_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})
_LOCK_METADATA_BYTES = 4096
_LOCK_OWNERLESS_STALE_SECONDS = 30.0
_LOCK_OWNERLESS_CONFIRM_SECONDS = 0.05
_LOCK_PID_PREFIX_RE = re.compile(rb'"pid"\s*:\s*([0-9]+)')
_LOCK_GENERATION_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_CONFIG_LOCK_TOKEN_PREFIX = b"\0agenttalk-config-lock-generation-v1:"

_AWAIT_SCHEMA_VERSION = 1
_MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION = 1
_AWAIT_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")
_AWAIT_PATH_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_AWAIT_SOURCES = frozenset({"send", "reply"})
_AWAIT_FIELDS = frozenset({
    "schema_version",
    "agent",
    "request_id",
    "wrapper_generation",
    "wait_token",
    "started_at",
    "source",
})
_AWAIT_MAX_RECORD_BYTES = 4096
_AWAIT_MAX_RECORDS_PER_AGENT = 64
_AWAIT_MAX_ROOT_ENTRIES = 256
_AWAIT_MAX_DIAGNOSTICS = 64


def _ensure_lock_byte(fd: int) -> None:
    """Make byte zero lockable without relying on owner metadata."""
    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\0")
        os.fsync(fd)


def _try_acquire_file_lock(fd: int) -> bool:
    """Try one non-blocking cross-platform exclusive byte/file lock."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _LOCK_BUSY_ERRNOS:
            return False
        raise
    return True


def _release_file_lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_all(fd: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short file write")
        remaining = remaining[written:]


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag)


def _validate_lock_file_stat(
    path: Path,
    info: os.stat_result,
    *,
    expected_links: int = 1,
) -> None:
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise OSError(f"unsafe lock path {path}: symlink or reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"unsafe lock path {path}: not a regular file")
    if info.st_nlink != expected_links:
        raise OSError(
            f"unsafe lock path {path}: hardlink count is {info.st_nlink}, "
            f"expected {expected_links}"
        )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _file_revision(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _open_existing_lock_file(
    path: Path,
    flags: int,
    *,
    expected_links: int = 1,
) -> tuple[int, os.stat_result]:
    """Open one existing lock path without following or accepting links."""
    before = os.lstat(path)
    _validate_lock_file_stat(path, before, expected_links=expected_links)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags | nofollow)
    try:
        opened = os.fstat(fd)
        _validate_lock_file_stat(path, opened, expected_links=expected_links)
        if not _same_file(before, opened):
            raise OSError(f"unsafe lock path {path}: pathname changed while opening")
        current = os.lstat(path)
        _validate_lock_file_stat(path, current, expected_links=expected_links)
        if not _same_file(opened, current):
            raise OSError(f"unsafe lock path {path}: pathname changed while opening")
        return fd, opened
    except BaseException:
        os.close(fd)
        raise


def _unlink_if_same_file(path: Path, expected: os.stat_result) -> bool:
    """Remove only ``expected`` without deleting a replacement generation.

    There is no portable compare-and-unlink syscall. Move the current pathname
    to a private quarantine first, then inspect what the atomic rename moved.
    If an old client replaced the path after our identity check, restore that
    generation with a no-replace hardlink instead of unlinking it.
    """
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(current.st_mode) or _is_reparse_point(current):
        return False
    if not _same_file(current, expected):
        return False
    quarantine = path.with_name(f".{path.name}.{uuid.uuid4().hex}.unlink")
    try:
        os.rename(path, quarantine)
    except FileNotFoundError:
        return True
    moved = os.lstat(quarantine)
    if _same_file(moved, expected):
        os.unlink(quarantine)
        return True

    if stat.S_ISLNK(moved.st_mode) or _is_reparse_point(moved):
        raise OSError(
            f"pathname generation changed while removing {path}; "
            f"replacement preserved at {quarantine}"
        )
    try:
        os.link(quarantine, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise OSError(
            f"pathname generation changed while removing {path}; "
            f"replacement preserved at {quarantine}"
        ) from exc
    os.unlink(quarantine)
    return False


def _recover_published_prepare_link(path: Path) -> bool:
    """Remove only a current-client prepare alias left by a post-link crash."""
    try:
        fd, identity = _open_existing_lock_file(
            path,
            os.O_RDONLY,
            expected_links=2,
        )
    except OSError:
        return False
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _LOCK_METADATA_BYTES)
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode("utf-8").strip(" \t\r\n\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(record, dict) or record.get("protocol") != "o_excl_v2":
        return False
    pid = record.get("pid")
    generation = record.get("generation")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(generation, str)
        or _LOCK_GENERATION_RE.fullmatch(generation) is None
    ):
        return False

    prepared = path.with_name(f".{path.name}.{generation}.prepare")
    try:
        prepared_identity = os.lstat(prepared)
        _validate_lock_file_stat(prepared, prepared_identity, expected_links=2)
    except OSError:
        return False
    if not _same_file(identity, prepared_identity):
        return False
    if not _unlink_if_same_file(prepared, identity):
        return False

    current = os.lstat(path)
    _validate_lock_file_stat(path, current)
    if not _same_file(identity, current):
        raise OSError(f"unsafe lock path {path}: generation changed during recovery")
    return True


def _read_lock_owner(path: Path) -> tuple[int | None, os.stat_result, dict | None]:
    fd, identity = _open_existing_lock_file(path, os.O_RDONLY)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _LOCK_METADATA_BYTES)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8").strip(" \t\r\n\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        match = _LOCK_PID_PREFIX_RE.search(raw)
        pid = int(match.group(1)) if match is not None else None
        return pid if pid and pid > 0 else None, identity, None
    pid = data.get("pid") if isinstance(data, dict) else None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    return pid, identity, data if isinstance(data, dict) else None


def _existing_os_lock_available(path: Path) -> bool:
    """Return False while a persistent-protocol peer holds the inode lock."""
    fd, _identity = _open_existing_lock_file(path, os.O_RDWR)
    acquired = False
    try:
        acquired = _try_acquire_file_lock(fd)
        return acquired
    finally:
        release_error: OSError | None = None
        if acquired:
            try:
                _release_file_lock(fd)
            except OSError as exc:
                release_error = exc
        try:
            os.close(fd)
        except OSError as exc:
            release_error = release_error or exc
        if release_error is not None:
            raise release_error


def _write_text_exclusive(path: Path, text: str) -> os.stat_result:
    """Durably create ``path`` once; never overwrite an existing generation."""
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    identity = os.fstat(fd)
    try:
        _write_all(fd, text.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
    except BaseException:
        cleanup_error: OSError | None = None
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if not _unlink_if_same_file(path, identity):
                cleanup_error = OSError(
                    f"could not clean partial exclusive file {path}: generation changed"
                )
        except OSError as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise OSError(
                f"could not clean partial exclusive file {path}: {cleanup_error}"
            ) from cleanup_error
        raise
    return identity


def _publish_text_no_replace(
    path: Path,
    text: str,
    *,
    prepare_token: str,
) -> os.stat_result:
    """Publish fully written text at an absent path without replacement.

    The public path never exists as a zero-byte current-client file: write and
    fsync a private inode first, then hardlink it atomically to the destination.
    Hardlink creation is no-replace on Windows and POSIX. The parent directory
    is fsynced after publishing the final name and removing the private alias.
    """
    if _LOCK_GENERATION_RE.fullmatch(prepare_token) is None:
        raise ValueError("prepare token must be a lowercase UUID hex value")
    prepared = path.with_name(f".{path.name}.{prepare_token}.prepare")
    identity = _write_text_exclusive(prepared, text)
    published = False
    try:
        os.link(prepared, path, follow_symlinks=False)
        published = True
        if not _unlink_if_same_file(prepared, identity):
            raise OSError(f"prepared publication generation changed at {prepared}")
        current = os.lstat(path)
        _validate_lock_file_stat(path, current)
        if not _same_file(identity, current):
            raise OSError("ownership marker generation changed during publish")
        _fsync_directory(path.parent)
        return current
    except BaseException:
        cleanup_error: OSError | None = None
        if published:
            try:
                if not _unlink_if_same_file(path, identity):
                    cleanup_error = OSError(
                        f"published file generation changed at {path}"
                    )
            except OSError as exc:
                cleanup_error = exc
        try:
            if not _unlink_if_same_file(prepared, identity):
                cleanup_error = cleanup_error or OSError(
                    f"prepared publication generation changed at {prepared}"
                )
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise OSError(
                f"could not clean failed publication for {path}: {cleanup_error}"
            ) from cleanup_error
        raise


def _fsync_directory(directory: Path) -> None:
    """Best-effort POSIX directory fsync after publishing a prepared file."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass

# Known message kinds. Receivers should silently skip anything else
# rather than letting an unfamiliar kind smuggle through to the LLM
# as a fresh instruction surface. New kinds must be added here AND
# documented in the skill bodies + CHANGELOG.
KNOWN_KINDS = frozenset({
    "message",
    "note",
    "question",
    "review-request",
    "review-result",
    # Proposal pair: one agent proposes a concrete solution/approach and
    # the peer accepts / rejects / counters. Distinct from `question`
    # (open-ended) and `review-request` (review of work already done).
    # Correlated via meta.request_id like the review pair (the `propose`
    # command mints a `pp-` id); a `proposal-response` carries
    # meta.status=accepted|rejected|countered. A counter is a fresh
    # `proposal` with meta.in_reply_to=<old request_id>. Added in 0.10.0.
    "proposal",
    "proposal-response",
    "wake",
    "end",
    # Loop-control signal: "stand down / exit your listen loop — we may
    # restart you later." Distinct from `end` (whole session over + transcript
    # export): `release` is lighter (no transcript) and the agent may be
    # re-armed. Deliberately NOT a control kind — `wait` must RETURN it so the
    # listener sees it and exits (same path as `end`); the exit decision lives
    # in the listen skill, not the bus. Opens no thread (not an opener kind).
    # Added for the listen-exit-clarity feature: a DEDICATED stop signal so a
    # prose "done for now" can never be misread as "stop listening".
    "release",
    # Control-plane kind: peer is still drafting a real reply. Receivers
    # treat these as a deadline-extension signal in `agenttalk wait` —
    # they do not surface as a returned reply. Added in 0.8.0 to fix
    # "reply landed seconds after wait timed out" sharp-edge.
    "composing",
    # A requester marks one of its own tracked requests as no-longer-
    # current. Correlated via meta.request_id (+ optional
    # meta.target_msg_id); thread derivation reports the thread as
    # `closed-superseded` and a scoped `wait` wakes with a distinct
    # rescinded outcome. Deliberately NOT a control kind: it changes
    # what other messages mean, so it must stay transcript-visible and
    # auditable. Added in 0.14.0 (issue #12 — the launch-HOLD/fire
    # crossing from the 2026-06-05 production retro).
    "rescind",
})

# Kinds the bus uses to signal flow control rather than carry agent
# content. They are still persisted (so transcripts and the dashboard
# can show them for audit), but `agenttalk wait` does not return them
# as a reply and `agenttalk recv` filters them out of the default view.
CONTROL_KINDS = frozenset({"composing"})

# Kinds that OPEN a trackable request/reply thread. Single source of
# truth shared by thread derivation (threads.py) and rescind validation
# (`validate_rescind`) — store.py cannot import threads.py (threads
# imports store), so the constant lives here and threads re-exports it.
OPENER_KINDS = frozenset({"review-request", "question", "proposal"})

# Reply-in-flight marker entries older than this are ignored by readers.
# Deliberately equal to the wait loop's cumulative composing-extension cap
# (cli._COMPOSING_MAX_EXTEND_SECONDS): if composing pings could not have
# held a waiter past this horizon, a drafting marker should not suppress
# staleness warnings past it either. One number, one meaning. (0.14.0, #14)
COMPOSING_INTENT_STALE_SECONDS = 1800.0

# Agent names are interpolated directly into filesystem paths
# (cursors, heartbeats), so they must be portable identifiers — not
# arbitrary user input. Allow alphanumerics plus dot / underscore /
# hyphen, must start with an alphanumeric, max 64 chars. Note: we
# deliberately use `\A...\Z` rather than `^...$` because Python's
# `$` anchor matches immediately before a trailing newline, which
# would let `"claude\n"` slip through into a state filename.
_AGENT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

# Session IDs are used as filesystem path components under
# .agenttalk/archived/<session_id>/, so they need the same kind of
# guard rail as agent names. Accept both the old format
# (YYYYMMDDTHHMMSSZ) and the new format (YYYYMMDDTHHMMSS-XXXXZ with
# a random suffix) so old configs from 0.3.x still validate.
_SESSION_ID_RE = re.compile(r"\A[0-9]{8}T[0-9]{6}(-[A-Za-z0-9]{4})?Z\Z")

# Sidecars that share the dead-letter sink alongside the <stem>.json payloads. They also
# end in .json, so every payload scan (list_dead_letters / dead_lettered_count) MUST exclude
# them or a sidecar leaks in as a phantom dead-letter with a bogus stem message_id. The
# .resolved.json sidecar was added in 0.56.0 (dead-letter resolve).
_DEAD_LETTER_SIDECAR_SUFFIXES = (".deadletter.json", ".resolved.json")
_LEAD_CHAT_AVAILABLE_STATES = frozenset({
    _health.STATE_IDLE_WAITING,
    _health.STATE_WORKING_TURN,
    _health.STATE_WORKING_SILENT,
})
_LEAD_CHAT_HEARTBEAT_STALE_AFTER_SECONDS = 120.0


def _is_dead_letter_payload(name: str) -> bool:
    return name.endswith(".json") and not name.endswith(_DEAD_LETTER_SIDECAR_SUFFIXES)


def validate_agent_name(name: str) -> str:
    """Return ``name`` if it's a safe agent identifier, else raise ValueError.

    Safe identifier: alphanumeric + dot/underscore/dash, starts with
    alphanumeric, 1–64 chars. Rejects path separators, ``..``, leading
    punctuation, whitespace (including trailing newlines/CRLF — a
    real bite that the `$` anchor would have missed), quotes, and
    anything else that could escape ``.agenttalk/state/`` when
    interpolated into a filename.
    """
    if not isinstance(name, str):
        raise ValueError(f"agent name must be a string, got {type(name).__name__}")
    if not name:
        raise ValueError("agent name cannot be empty")
    if name.casefold() in _avatars.RESERVED_PRINCIPALS:
        raise ValueError(
            f"agent name {name!r} is reserved for the {name.casefold()} principal"
        )
    if not _AGENT_NAME_RE.match(name):
        raise ValueError(
            f"agent name {name!r} is not a safe identifier "
            f"(allowed: alphanumeric plus . _ -, must start with a letter "
            f"or digit, max 64 chars)"
        )
    return name


def validate_session_id(session_id: str) -> str:
    """Return ``session_id`` if it's a safe filesystem-path fragment.

    `reset --archive` writes to ``.agenttalk/archived/<session_id>/``,
    so a corrupted config with ``session_id="../escaped"`` could
    archive outside the archive root. This validator rejects anything
    that isn't a generated session id (old or new format).
    """
    if not isinstance(session_id, str):
        raise ValueError(
            f"session_id must be a string, got {type(session_id).__name__}"
        )
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"session_id {session_id!r} is not a safe identifier "
            f"(expected YYYYMMDDTHHMMSSZ or YYYYMMDDTHHMMSS-XXXXZ)"
        )
    return session_id


def validate_agent_roster(names: list[str]) -> list[str]:
    """Validate each name AND check uniqueness across the roster.

    Uniqueness is **case-insensitive** because agent names are used as
    filename stems on filesystems that are case-insensitive by default
    (NTFS, default macOS). Without this, `--agents Alpha,alpha` would
    create one shared `Alpha.cursor` file with two logical owners.
    """
    seen: dict[str, str] = {}  # casefolded -> original
    for n in names:
        validate_agent_name(n)
        key = n.casefold()
        if key in seen:
            other = seen[key]
            if other == n:
                raise ValueError(
                    f"agent name {n!r} appears more than once in the roster"
                )
            raise ValueError(
                f"agent names {other!r} and {n!r} only differ by case; on "
                f"case-insensitive filesystems they would alias the same "
                f"state files. Pick distinct names."
            )
        seen[key] = n
    return names


def _bus_principals(roster: list[str]) -> set[str]:
    """Active agents plus code-owned reserved principals that may appear on the bus."""
    return set(roster) | set(_avatars.RESERVED_PRINCIPALS)


# Group names share the agent-name safety rule (interpolated nowhere
# dangerous today, but kept portable), with one reservation: "all" is the
# implicit whole-roster audience and may not be redefined.
_RESERVED_GROUP_NAMES = frozenset({"all"})


def validate_group_name(name: str) -> str:
    """Return ``name`` if it's a safe, non-reserved group identifier."""
    if not isinstance(name, str) or not name:
        raise ValueError("group name must be a non-empty string")
    if name.casefold() in _RESERVED_GROUP_NAMES:
        raise ValueError(
            f"group name {name!r} is reserved ('all' is the implicit "
            f"whole-roster audience)"
        )
    if not _AGENT_NAME_RE.match(name):
        raise ValueError(
            f"group name {name!r} is not a safe identifier "
            f"(allowed: alphanumeric plus . _ -, must start with a letter "
            f"or digit, max 64 chars)"
        )
    return name


def validate_groups(groups: dict, roster: list[str]) -> dict:
    """Validate a ``{group: [members]}`` map against the roster.

    Every group name must be safe + non-reserved, every value a list, and
    every member must be in the roster (so a broadcast can never fan out
    to a phantom mailbox).
    """
    if not isinstance(groups, dict):
        raise ValueError(f"'groups' must be a dict, got {type(groups).__name__}")
    rset = set(roster)
    for gname, members in groups.items():
        validate_group_name(gname)
        if not isinstance(members, list):
            raise ValueError(f"group {gname!r} members must be a list")
        for m in members:
            if not isinstance(m, str):
                raise ValueError(f"group {gname!r} member must be a string")
            # Fail CLOSED even on an empty roster: a config with no agents
            # has no valid group members, so any member reference is bogus.
            if m not in rset:
                raise ValueError(
                    f"group {gname!r} member {m!r} is not in the roster {sorted(rset)}"
                )
    return groups


def validate_roles(roles: dict, roster: list[str]) -> dict:
    """Validate a ``{agent: role}`` map: keys in roster, values bounded strings."""
    if not isinstance(roles, dict):
        raise ValueError(f"'roles' must be a dict, got {type(roles).__name__}")
    rset = set(roster)
    for agent, role in roles.items():
        if agent not in rset:  # fail closed even on an empty roster
            raise ValueError(f"role key {agent!r} is not in the roster {sorted(rset)}")
        if not isinstance(role, str) or not role:
            raise ValueError(f"role for {agent!r} must be a non-empty string")
        if len(role) > 64 or not role.isprintable():
            raise ValueError(
                f"role for {agent!r} must be a printable string of at most 64 chars"
            )
    return roles


TRUST_CLASS_EXTERNAL_WORKER = "external-worker"
KNOWN_TRUST_CLASSES = frozenset({TRUST_CLASS_EXTERNAL_WORKER})


def validate_trust_classes(trust_classes: dict, roster: list[str]) -> dict:
    """Validate opt-in model trust metadata for active roster identities."""
    if not isinstance(trust_classes, dict):
        raise ValueError(
            f"'trust_classes' must be a dict, got {type(trust_classes).__name__}"
        )
    rset = set(roster)
    for agent, trust_class in trust_classes.items():
        if agent not in rset:
            raise ValueError(
                f"trust class key {agent!r} is not in the roster {sorted(rset)}"
            )
        if trust_class not in KNOWN_TRUST_CLASSES:
            raise ValueError(
                f"trust class for {agent!r} must be one of {sorted(KNOWN_TRUST_CLASSES)}"
            )
    return trust_classes


def validate_managed_lead_loop(managed: object, roster: list[str]) -> dict:
    """Validate the ``{agent: {enabled, ttl_seconds, cadence_seconds}}`` map.

    Keys must be in the roster; each value an object with an optional bool
    ``enabled`` and positive-number ``ttl_seconds`` / ``cadence_seconds``. The
    lease TTL must EXCEED the renew cadence so a single missed renewal (a long
    turn, a brief stall) cannot expire a healthy controller. Fail-closed so a
    corrupt config cannot mark a phantom identity managed or smuggle a
    non-numeric lease bound. Generic by AGENT NAME - never keyed on a cli."""
    if not isinstance(managed, dict):
        raise ValueError(f"'managed_lead_loop' must be a dict, got {type(managed).__name__}")
    rset = set(roster)
    for agent, spec in managed.items():
        if agent not in rset:  # fail closed even on an empty roster
            raise ValueError(
                f"managed_lead_loop key {agent!r} is not in the roster {sorted(rset)}")
        if not isinstance(spec, dict):
            raise ValueError(f"managed_lead_loop[{agent!r}] must be an object")
        if "enabled" in spec and not isinstance(spec["enabled"], bool):
            raise ValueError(f"managed_lead_loop[{agent!r}].enabled must be a bool")
        nums = {}
        for k in ("ttl_seconds", "cadence_seconds"):
            if k in spec:
                v = spec[k]
                # Reject bool (a bool IS an int), non-numbers, and NON-FINITE values:
                # `v <= 0` is False for both NaN and +inf, so without the isfinite gate
                # they slip through -> NaN serializes to an INVALID JSON token and makes
                # expiry math permanently wrong (NaN -> never-expired diagnostic; +inf ->
                # an un-stealable dead owner). isfinite only runs after the numeric check.
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not math.isfinite(v) or v <= 0:
                    raise ValueError(
                        f"managed_lead_loop[{agent!r}].{k} must be a finite positive number")
                nums[k] = v
        if "ttl_seconds" in nums and "cadence_seconds" in nums \
                and nums["ttl_seconds"] <= nums["cadence_seconds"]:
            raise ValueError(
                f"managed_lead_loop[{agent!r}].ttl_seconds must exceed cadence_seconds")
    return managed


def validate_retired(retired: object, active_roster: list[str]) -> list:
    """Validate the ``retired`` registry (0.16.0, #19 Phase A).

    A list of tombstone objects, one per retired identity:
    ``{"name", "retired_at", "renamed_to": str|None, "reason": str|None}``.
    Fail-closed so a corrupt registry can't put a name in both the active
    roster and the tombstone list (an identity is active XOR retired), smuggle
    an unsafe name into filename interpolation, or duplicate a tombstone. The
    disjointness + uniqueness checks are **case-insensitive** for the same
    filesystem-aliasing reason as ``validate_agent_roster``: a retired name must
    be unrepresentable as a new active identity (FR-002 non-rebindable).
    """
    if not isinstance(retired, list):
        raise ValueError(f"'retired' must be a list, got {type(retired).__name__}")
    active_keys = {a.casefold() for a in active_roster}
    seen: dict[str, str] = {}
    for e in retired:
        if not isinstance(e, dict):
            raise ValueError(
                f"each 'retired' entry must be an object, got {type(e).__name__}"
            )
        name = e.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("a 'retired' entry's 'name' must be a non-empty string")
        validate_agent_name(name)
        rn = e.get("renamed_to")
        if rn is not None:
            if not isinstance(rn, str) or not rn:
                raise ValueError(
                    f"retired {name!r}: 'renamed_to' must be a non-empty string or null"
                )
            validate_agent_name(rn)
        key = name.casefold()
        if key in active_keys:
            raise ValueError(
                f"identity {name!r} is in BOTH the active roster and 'retired' "
                f"(an identity is active XOR retired, never both)"
            )
        if key in seen:
            raise ValueError(
                f"retired identity {name!r} (or a case-variant {seen[key]!r}) "
                f"appears more than once — duplicate tombstone"
            )
        seen[key] = name
    return retired


@dataclass
class Message:
    id: str
    ts: str
    sender: str
    recipient: str
    kind: str = "message"
    subject: str = ""
    body: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("sender")
        d["to"] = d.pop("recipient")
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Construct from a trusted dict.

        For untrusted on-disk JSON, prefer ``Message.from_raw()`` —
        it does strict schema validation before construction, so a
        malformed file can't smuggle a numeric `id` or missing `ts`
        into the Store and crash downstream callers.
        """
        return cls(
            id=data["id"],
            ts=data["ts"],
            sender=data.get("from", data.get("sender", "")),
            recipient=data.get("to", data.get("recipient", "")),
            kind=data.get("kind", "message"),
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            meta=data.get("meta", {}) or {},
        )

    @classmethod
    def from_raw(cls, data) -> "Message":
        """Strict construction from untrusted JSON.

        Raises ``ValueError`` with a human-readable reason for any
        shape/type/missing-field failure. The single entry point from
        ``.agenttalk/messages/*.json`` files into the in-memory bus
        — see ``Store.all_messages()``.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"top-level value must be a JSON object, got {type(data).__name__}"
            )
        for fname in ("id", "ts"):
            if fname not in data:
                raise ValueError(f"missing required field {fname!r}")
        for fname in ("id", "ts"):
            if not isinstance(data[fname], str) or not data[fname]:
                raise ValueError(
                    f"field {fname!r} must be a non-empty string, "
                    f"got {type(data[fname]).__name__}"
                )
        # The id must be a real generated id. A hand-written or corrupt id
        # of the wrong shape (e.g. "zzzz") would otherwise validate, deliver,
        # and — once acked — poison the recipient's cursor, since delivery
        # ordering is a lexicographic compare of ids (0.18.0).
        if not _ID_RE.match(data["id"]):
            raise ValueError(
                f"malformed id {data['id']!r} (not a generated message id)"
            )
        for fname in ("kind", "subject", "body"):
            if fname in data and not isinstance(data[fname], str):
                raise ValueError(
                    f"field {fname!r} must be a string if present, "
                    f"got {type(data[fname]).__name__}"
                )
        sender = data.get("from", data.get("sender"))
        recipient = data.get("to", data.get("recipient"))
        if not isinstance(sender, str) or not sender:
            raise ValueError("field 'from' must be a non-empty string")
        if not isinstance(recipient, str) or not recipient:
            raise ValueError("field 'to' must be a non-empty string")
        if "meta" in data and not isinstance(data["meta"], dict):
            raise ValueError(
                f"field 'meta' must be a dict, got {type(data['meta']).__name__}"
            )
        return cls.from_dict(data)

    def validate(self, roster: list[str]) -> None:
        """Raise ValueError if this message fails schema/roster checks.

        Strict schema validation has two purposes:
        1. Data integrity: catches bugs and disk corruption (a
           message file with the wrong shape never gets handled).
        2. Reducing the attack surface: unknown kinds can't smuggle
           an unfamiliar verb into the LLM's instruction set.

        This does NOT defend against an attacker who can write
        well-formed messages — that's a signing problem (see
        SECURITY.md). It does mean such an attacker has to pick from
        the known-kind vocabulary, which is small and well-understood.
        """
        if self.kind not in KNOWN_KINDS:
            raise ValueError(
                f"unknown kind {self.kind!r} (known: {sorted(KNOWN_KINDS)})"
            )
        if not isinstance(self.body, str):
            raise ValueError(f"body must be a string, got {type(self.body).__name__}")
        if not isinstance(self.meta, dict):
            raise ValueError(f"meta must be a dict, got {type(self.meta).__name__}")
        _gates.validate_response_status(self.kind, self.meta)
        if roster:
            principals = _bus_principals(roster)
            if self.sender not in principals:
                raise ValueError(
                    f"sender {self.sender!r} not in bus principals {sorted(principals)}"
                )
            if self.recipient not in principals:
                raise ValueError(
                    f"recipient {self.recipient!r} not in bus principals {sorted(principals)}"
                )


@dataclass(frozen=True)
class OperatorAnswerSendResult:
    ok: bool
    message: Message | None = None
    denial_code: str | None = None
    detail: str = ""
    failed: bool = False


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.dir = self.root / DIRNAME
        self.messages_dir = self.dir / "messages"
        self.state_dir = self.dir / "state"
        self.sessions_dir = self.dir / "sessions"
        self.config_path = self.dir / "config.json"

    # ------------------------------------------------------------------ init

    def initialized(self) -> bool:
        return self.config_path.exists()

    def init(self, agents: list[str], *, force: bool = False) -> dict:
        validate_agent_roster(agents)
        if self.initialized() and not force:
            return self.load_config()
        # #19: retired tombstones are PERMANENT and non-rebindable (FR-002) —
        # by EVERY registry operation, including `init --force`. Preserve the
        # existing `retired` list across a force re-init and refuse a new roster
        # that collides (case-insensitively) with a tombstone, so `init --force`
        # can't silently resurrect a retired identity (fresh-eyes review). If
        # the old config is unreadable (the documented force-recovery case),
        # there is nothing safe to carry forward.
        # Read the existing tombstones DEFENSIVELY from the raw config JSON —
        # NOT via load_config(), which fails on any corruption. A tombstone that
        # is PRESENT but in a validation-failed config (e.g. an attacker put the
        # retired name back into `agents`) must still be preserved + protected,
        # else `init --force` becomes a tombstone-clearing bypass (Codex review
        # of the fresh-eyes fix). Each carried entry is sanitized to a clean,
        # re-validatable tombstone; only a config damaged beyond JSON-parse has
        # nothing recoverable to carry.
        retired_carry: list = []
        external_carry: set[str] = set()
        if self.initialized():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw = None
            raw_list = raw.get("retired") if isinstance(raw, dict) else None
            if isinstance(raw_list, list):
                seen_keys: set[str] = set()
                for e in raw_list:
                    name = e.get("name") if isinstance(e, dict) else None
                    if not (isinstance(name, str) and name):
                        continue
                    try:
                        validate_agent_name(name)
                    except ValueError:
                        continue  # drop an unsafe tombstone name
                    if name.casefold() in seen_keys:
                        continue  # drop a duplicate tombstone
                    seen_keys.add(name.casefold())
                    rn = e.get("renamed_to")
                    if rn is not None:
                        try:
                            validate_agent_name(rn)
                        except (ValueError, TypeError):
                            rn = None  # drop an unsafe successor pointer
                    retired_carry.append({
                        "name": name,
                        "retired_at": (e.get("retired_at")
                                       if isinstance(e.get("retired_at"), str)
                                       else _now_iso()),
                        "renamed_to": rn,
                        "reason": (e.get("reason")
                                   if isinstance(e.get("reason"), str) else None),
                    })
            raw_trust = raw.get("trust_classes") if isinstance(raw, dict) else None
            if isinstance(raw_trust, dict):
                for name, trust_class in raw_trust.items():
                    if trust_class != TRUST_CLASS_EXTERNAL_WORKER:
                        continue
                    try:
                        validate_agent_name(name)
                    except (TypeError, ValueError):
                        continue
                    external_carry.add(name)
        retired_keys = {
            e["name"].casefold()
            for e in retired_carry
            if isinstance(e, dict) and isinstance(e.get("name"), str)
        }
        active_external: list[str] = []
        for historical_name in sorted(external_carry):
            matches = [
                name for name in agents
                if name.casefold() == historical_name.casefold()
            ]
            if matches:
                if matches[0] != historical_name:
                    raise ValueError(
                        f"cannot init with case-variant {matches[0]!r} of historical "
                        f"external-worker {historical_name!r}; identities are non-rebindable"
                    )
                active_external.append(historical_name)
            elif historical_name.casefold() not in retired_keys:
                retired_carry.append({
                    "name": historical_name,
                    "retired_at": _now_iso(),
                    "renamed_to": None,
                    "reason": "external-worker omitted by force init",
                })
                retired_keys.add(historical_name.casefold())
        if retired_carry:
            tomb_keys = {
                e["name"].casefold() for e in retired_carry
                if isinstance(e, dict) and isinstance(e.get("name"), str)
            }
            clash = sorted({a for a in agents if a.casefold() in tomb_keys})
            if clash:
                raise ValueError(
                    f"cannot init with {clash}: still a retired tombstone — "
                    f"tombstones are permanent and non-rebindable (#19). Pick "
                    f"different names, or remove the .agenttalk/ directory "
                    f"entirely to start fully fresh."
                )
        for d in (self.messages_dir, self.state_dir, self.sessions_dir):
            d.mkdir(parents=True, exist_ok=True)
        cfg = {
            "agents": agents,
            "created_at": _now_iso(),
            "session_id": _new_session_id(),
            "operator_identity": _avatars.OPERATOR_PRINCIPAL,
            "deadman": {"mail_age_slo_seconds": 900, "alarm_unread_response": False},
            # NOTE: no project_id in config.json. The HMAC key file
            # is addressed by `signing.project_id_for_root(self.root)`,
            # a path-derived hash that an attacker writing into
            # .agenttalk/ cannot influence. See SECURITY.md.
        }
        if retired_carry:
            cfg["retired"] = retired_carry  # tombstones survive a force re-init
        if active_external:
            cfg["trust_classes"] = dict.fromkeys(
                active_external, TRUST_CLASS_EXTERNAL_WORKER
            )
        _atomic_write_text(self.config_path, json.dumps(cfg, indent=2))
        for a in agents:
            cur = self.state_dir / f"{a}.cursor"
            if not cur.exists():
                _atomic_write_text(cur, "")
        return cfg

    def reset(self, *, archive: bool = False) -> tuple[dict, Path | None]:
        """Clear active bus state (messages, cursors, heartbeats);
        start a new session.

        ``init --force`` rewrites the config but intentionally keeps
        state. When the user really wants a clean slate they call
        this explicitly.

        Default behavior:
        - **deletes** ``messages/``, ``state/``, and ``checkpoints/``
          (active bus and context-resume state)
        - **preserves** ``sessions/`` (historical transcript exports
          — those are user-visible artifacts, not active bus state)
        - bumps ``session_id``

        With ``archive=True``:
        - **moves** ``messages/``, ``state/``, ``checkpoints/``, AND
          ``sessions/`` into
          ``.agenttalk/archived/<old_session_id>/`` so the full prior
          session is recoverable.

        Returns ``(new_config, archive_path_or_None)``.
        """
        if not self.initialized():
            raise FileNotFoundError(
                f"agenttalk not initialized in {self.root}. Nothing to reset."
            )
        cfg = self.load_config()  # validates session_id format
        old_session_id = cfg.get("session_id", "unknown")

        archive_path: Path | None = None
        if archive:
            # Archive everything including past transcripts
            archive_path = self._archive_session(
                old_session_id,
                subdirs=("messages", "state", "checkpoints", "sessions"),
            )
        else:
            # Default delete: active bus + resume state only. sessions/ holds
            # exported transcripts (a user-visible artifact) — keep them.
            for sub in (
                self.messages_dir,
                self.state_dir,
                self.dir / "checkpoints",
            ):
                if sub.exists():
                    shutil.rmtree(sub)

        # Recreate active-state dirs + cursor files so the bus is
        # immediately usable
        for d in (self.messages_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        cfg["session_id"] = _new_session_id()
        cfg["created_at"] = _now_iso()
        _atomic_write_text(self.config_path, json.dumps(cfg, indent=2))

        for a in cfg.get("agents", []):
            cur = self.state_dir / f"{a}.cursor"
            if not cur.exists():
                _atomic_write_text(cur, "")
        return cfg, archive_path

    def _archive_session(self, session_id: str,
                         subdirs: tuple[str, ...] = ("messages", "state", "sessions")) -> Path:
        """Move named subdirs into archived/<session_id>/.

        Validates ``session_id`` as a safe path fragment before
        constructing the archive path, so a corrupt
        ``config.json[session_id]`` cannot escape ``archived/``.

        Uses ``shutil.move`` (same-filesystem rename) so the operation
        is fast even on large message dirs. The archive is read-only
        once moved — agenttalk never writes into ``archived/``.
        """
        validate_session_id(session_id)  # fail-closed against traversal
        archive_dir = self.dir / "archived" / session_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        for sub in subdirs:
            src = self.dir / sub
            if src.exists():
                dst = archive_dir / sub
                # If a previous archive collision exists, move into a
                # sub-subdir tagged with a timestamp to never destroy
                # archived data.
                if dst.exists():
                    dst = archive_dir / f"{sub}.{_now_iso().replace(':', '-')}"
                shutil.move(str(src), str(dst))
        return archive_dir

    def load_config(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"agenttalk not initialized in {self.root}. Run `agenttalk init` first."
            )
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError(
                f"corrupt config at {self.config_path}: root must be a JSON object"
            )
        # Validate roster on load so a malformed config can't smuggle
        # unsafe names through to downstream filename interpolation.
        agents = cfg.get("agents")
        if not isinstance(agents, list):
            raise ValueError(
                f"corrupt config at {self.config_path}: 'agents' must be a list"
            )
        try:
            validate_agent_roster(agents)
        except ValueError as e:
            raise ValueError(
                f"corrupt config at {self.config_path}: {e}. "
                f"Re-init with `agenttalk init --here --agents ...`."
            ) from e
        # session_id is interpolated into archive paths, so reject
        # corrupt values at load time rather than crashing in reset.
        sid = cfg.get("session_id")
        if sid is not None:
            try:
                validate_session_id(sid)
            except ValueError as e:
                raise ValueError(
                    f"corrupt config at {self.config_path}: {e}. "
                    f"Re-init with `agenttalk init --here --agents ... --force`."
                ) from e
        # Optional team metadata (added in 0.11.0). Absent OR explicit null
        # ⇒ pair behavior (matches the `(... or {})` accessors). Validate
        # fail-closed so a corrupt groups/roles map can't fan a broadcast
        # out to a phantom mailbox or crash the roster view.
        if cfg.get("groups") is not None:
            try:
                validate_groups(cfg["groups"], agents)
            except ValueError as e:
                raise ValueError(f"corrupt config at {self.config_path}: {e}.") from e
        if cfg.get("roles") is not None:
            try:
                validate_roles(cfg["roles"], agents)
            except ValueError as e:
                raise ValueError(f"corrupt config at {self.config_path}: {e}.") from e
        if cfg.get("trust_classes") is not None:
            try:
                validate_trust_classes(cfg["trust_classes"], agents)
            except ValueError as e:
                raise ValueError(f"corrupt config at {self.config_path}: {e}.") from e
        # Identity registry tombstones (0.16.0, #19 Phase A). Absent OR null ⇒
        # no retirements (full 0.15.0 behavior). Validated fail-closed so a
        # corrupt registry can't alias an active name or smuggle an unsafe one.
        if cfg.get("retired") is not None:
            try:
                validate_retired(cfg["retired"], agents)
            except ValueError as e:
                raise ValueError(f"corrupt config at {self.config_path}: {e}.") from e
        # Managed lead-loop registry (lead-loop Slice 1). Absent OR null => no
        # managed identities. Validated fail-closed (positive lease bounds,
        # TTL > cadence, keys in roster) - generic by agent name, never by cli.
        if cfg.get("managed_lead_loop") is not None:
            mll = cfg["managed_lead_loop"]
            if isinstance(mll, dict):
                # SELF-HEAL a dangling key: if a roster member that was managed gets
                # removed/retired/renamed, a stale managed_lead_loop key would make
                # validate_managed_lead_loop RAISE -> every command exits 2, INCLUDING
                # the `managed-lead-loop clear` that would fix it (it load_config's
                # first). Prune non-roster keys IN-MEMORY so the tool stays usable; the
                # next config write persists the prune. Read-only here by design (this
                # path is called everywhere, often under a lock). Warn once per process
                # (default warning filter dedups by call site) for operator visibility.
                dangling = [k for k in mll if k not in agents]
                if dangling:
                    for k in dangling:
                        mll.pop(k, None)
                    warnings.warn(
                        f"config at {self.config_path}: pruned managed_lead_loop "
                        f"key(s) {sorted(dangling)} not in the roster (self-heal); the "
                        f"next roster/managed-lead-loop write persists this.",
                        stacklevel=2,
                    )
            try:
                validate_managed_lead_loop(cfg["managed_lead_loop"], agents)
            except ValueError as e:
                raise ValueError(f"corrupt config at {self.config_path}: {e}.") from e
        # Upgrade pre-0.68.0 single-operator stores without weakening the send
        # resolver: infer the dedicated reserved operator principal only when
        # lead-chat can resolve a lead and the roster has no collision. This
        # runs inside load_config, so mirror lead_chat_lead inline from cfg.
        if "operator_identity" not in cfg:
            effective_lead = None
            operator_facing = cfg.get("operator_facing")
            if isinstance(operator_facing, str) and operator_facing in agents:
                effective_lead = operator_facing
            else:
                roles = cfg.get("roles") or {}
                leads = [
                    a for a in agents
                    if isinstance(roles.get(a), str)
                    and roles[a].casefold() == "lead"
                ]
                if len(leads) == 1:
                    effective_lead = leads[0]
            if effective_lead is not None and _avatars.OPERATOR_PRINCIPAL not in agents:
                cfg["operator_identity"] = _avatars.OPERATOR_PRINCIPAL
        return cfg

    # ------------------------------------------------------- team / roster

    def _write_config(self, cfg: dict) -> None:
        _atomic_write_text(self.config_path, json.dumps(cfg, indent=2))

    # --- config mutation lock (review M2) -----------------------------------
    #
    # config.json is shared mutable state that any agent legitimately writes
    # (roster admin: add/remove/set-role/set-group/set-operator-facing/retire/
    # rename). _write_config is an atomic single-file replace, but the
    # surrounding load -> mutate -> write is NOT atomic, so two concurrent admin
    # ops both read the same base and the later writer silently clobbers the
    # earlier's change (a dropped retire/rename can even re-open a name #19
    # promises is permanent). There is no lock server, so serialize those
    # critical sections with a legacy-compatible O_EXCL ownership marker.
    # Per-agent cursor/threadstate/heartbeat writes are deliberately
    # NOT locked: they are single-writer under the documented one-window-per-
    # agent model; only shared config.json needs this.

    @contextlib.contextmanager
    def _lock_generation_guard(
        self,
        lock: Path,
        *,
        deadline: float,
        poll: float,
        what: str,
    ):
        """Serialize pathname-generation changes among current clients.

        The guard is a persistent OS-locked byte. Legacy clients ignore it but
        still interoperate through the public O_EXCL ownership marker.
        """
        guard = lock.with_name(f".{lock.name}.generation")
        fd: int | None = None
        acquired = False
        try:
            while fd is None:
                try:
                    fd = os.open(
                        str(guard),
                        os.O_CREAT | os.O_EXCL | os.O_RDWR,
                        0o600,
                    )
                    created = os.fstat(fd)
                    _validate_lock_file_stat(guard, created)
                except FileExistsError:
                    try:
                        fd, _identity = _open_existing_lock_file(guard, os.O_RDWR)
                    except FileNotFoundError:
                        continue
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not open the generation guard for {what} "
                            f"at {guard}"
                        ) from None
                    time.sleep(poll)
            _ensure_lock_byte(fd)
            guard_identity = os.fstat(fd)
            _validate_lock_file_stat(guard, guard_identity)
            current_guard = os.lstat(guard)
            _validate_lock_file_stat(guard, current_guard)
            if not _same_file(guard_identity, current_guard):
                raise OSError(
                    f"unsafe lock path {guard}: generation changed before locking"
                )
            while not _try_acquire_file_lock(fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire the generation guard for {what} "
                        f"at {guard}"
                    ) from None
                time.sleep(poll)
            acquired = True
            current_guard = os.lstat(guard)
            _validate_lock_file_stat(guard, current_guard)
            if not _same_file(guard_identity, current_guard):
                raise OSError(
                    f"unsafe lock path {guard}: generation changed while locking"
                )
            yield fd
            current_guard = os.lstat(guard)
            _validate_lock_file_stat(guard, current_guard)
            if not _same_file(guard_identity, current_guard):
                raise OSError(
                    f"unsafe lock path {guard}: generation changed while held"
                )
        finally:
            release_error: OSError | None = None
            if acquired and fd is not None:
                try:
                    _release_file_lock(fd)
                except OSError as exc:
                    release_error = exc
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as exc:
                    release_error = release_error or exc
            if release_error is not None:
                raise OSError(
                    f"could not release the generation guard for {what} "
                    f"at {guard}: {release_error}"
                ) from release_error

    @contextlib.contextmanager
    def _exclusive_lock(self, lock: Path, *, timeout: float = 10.0,
                        poll: float = 0.05, what: str = "lock"):
        """Hold a legacy-compatible O_EXCL marker across a critical section.

        Current clients serialize stale recovery and owner release with an
        OS-locked generation guard, eliminating their read/replace ABA window.
        Existing lock paths are read-only until validated as ordinary,
        single-link files; owner metadata is written only to an inode created
        by this process. Ownerless legacy crash remnants become recoverable
        only after an explicit conservative stale age plus a stable-generation
        observation. NOT re-entrant.
        """
        self._ensure_plain_lock_directory(
            lock.parent,
            what=f"{what} parent directory",
        )
        deadline = time.monotonic() + timeout
        identity: os.stat_result | None = None
        ownerless_generation: tuple[int, int] | None = None
        ownerless_seen_at: float | None = None
        try:
            while identity is None:
                with self._lock_generation_guard(
                    lock,
                    deadline=deadline,
                    poll=poll,
                    what=what,
                ):
                    generation = uuid.uuid4().hex
                    try:
                        created = _publish_text_no_replace(
                            lock,
                            json.dumps({
                                "pid": os.getpid(),
                                "protocol": "o_excl_v2",
                                "generation": generation,
                                "at": _now_iso(),
                                "root": str(self.root)[:512],
                            }, ensure_ascii=False),
                            prepare_token=generation,
                        )
                    except FileExistsError:
                        _recover_published_prepare_link(lock)
                        try:
                            pid, existing, record = _read_lock_owner(lock)
                        except FileNotFoundError:
                            continue
                        except PermissionError:
                            # A Windows byte lock can deny reads of the locked
                            # region. That is positive evidence of an active
                            # persistent-protocol holder: wait, never recover.
                            ownerless_generation = None
                            ownerless_seen_at = None
                        else:
                            os_lock_available = (
                                existing.st_size > 0
                                and _existing_os_lock_available(lock)
                            )
                            persistent_v1 = (
                                os_lock_available
                                and existing.st_size == _LOCK_METADATA_BYTES
                                and isinstance(record, dict)
                                and isinstance(record.get("generation"), str)
                                and "protocol" not in record
                            )
                            ownerless_candidate = (
                                pid is None
                                and (existing.st_size == 0 or os_lock_available)
                            )
                            existing_generation = (existing.st_dev, existing.st_ino)
                            ownerless_stale = (
                                ownerless_candidate
                                and time.time() - existing.st_mtime
                                >= _LOCK_OWNERLESS_STALE_SECONDS
                            )
                            if ownerless_stale:
                                observed_at = time.monotonic()
                                if ownerless_generation != existing_generation:
                                    ownerless_generation = existing_generation
                                    ownerless_seen_at = observed_at
                                ownerless_old_enough = (
                                    ownerless_seen_at is not None
                                    and observed_at - ownerless_seen_at
                                    >= _LOCK_OWNERLESS_CONFIRM_SECONDS
                                )
                            else:
                                ownerless_generation = None
                                ownerless_seen_at = None
                                ownerless_old_enough = False
                            owner_dead = (
                                pid is not None
                                and (existing.st_size == 0 or os_lock_available)
                                and _process_liveness(pid) == PROC_DEAD
                            )
                            if persistent_v1 or ownerless_old_enough or owner_dead:
                                if not _unlink_if_same_file(lock, existing):
                                    continue
                    else:
                        identity = os.lstat(lock)
                        try:
                            _validate_lock_file_stat(lock, identity)
                            if not _same_file(created, identity):
                                raise OSError(
                                    "ownership marker generation changed during create"
                                )
                        except OSError:
                            _unlink_if_same_file(lock, created)
                            identity = None
                            raise
                if identity is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not acquire the {what} at {lock} within "
                            f"{timeout:g}s"
                        ) from None
                    time.sleep(poll)
            yield
        finally:
            if identity is not None:
                with self._lock_generation_guard(
                    lock,
                    deadline=max(deadline, time.monotonic() + timeout),
                    poll=poll,
                    what=what,
                ):
                    last_error: OSError | None = None
                    for _ in range(100):
                        try:
                            current = os.lstat(lock)
                            _validate_lock_file_stat(lock, current)
                            if not _same_file(identity, current):
                                raise OSError("ownership marker generation changed")
                            if not _unlink_if_same_file(lock, identity):
                                raise OSError("ownership marker generation changed")
                            last_error = None
                            break
                        except FileNotFoundError as exc:
                            last_error = exc
                            break
                        except PermissionError as exc:
                            last_error = exc
                            time.sleep(0.01)
                        except OSError as exc:
                            last_error = exc
                            break
                    if last_error is not None:
                        raise OSError(
                            f"could not release the {what} at {lock}: {last_error}"
                        ) from last_error

    def _advance_config_lock_generation(
        self,
        lock: Path,
        *,
        timeout: float,
        poll: float,
    ) -> tuple[str | None, str]:
        """Advance the durable current-client config acquisition token."""
        deadline = time.monotonic() + timeout
        with self._lock_generation_guard(
            lock,
            deadline=deadline,
            poll=poll,
            what="config lock generation",
        ) as fd:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 256)
            previous: str | None = None
            if raw.startswith(_CONFIG_LOCK_TOKEN_PREFIX):
                candidate = raw[len(_CONFIG_LOCK_TOKEN_PREFIX):]
                try:
                    decoded = candidate.decode("ascii")
                except UnicodeDecodeError:
                    decoded = ""
                if _LOCK_GENERATION_RE.fullmatch(decoded):
                    previous = decoded
            current = uuid.uuid4().hex
            payload = _CONFIG_LOCK_TOKEN_PREFIX + current.encode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all(fd, payload)
            os.fsync(fd)
            return previous, current

    @contextlib.contextmanager
    def _config_lock(self, *, timeout: float = 10.0, poll: float = 0.05):
        """Hold a versioned exclusive config read-modify-write transaction."""
        self._ensure_plain_lock_directory(
            self.dir,
            what="AgentTalk runtime lock directory",
        )
        lock = self.dir / "config.lock"
        with self._exclusive_lock(
            lock,
            timeout=timeout,
            poll=poll,
            what="config lock (another agent may be mid roster-admin)",
        ):
            yield self._advance_config_lock_generation(
                lock,
                timeout=timeout,
                poll=poll,
            )

    def config_lock(self, *, timeout: float = 10.0, poll: float = 0.05):
        """Public alias for the config read-modify-write lock.

        Delegates to ``_config_lock`` so a
        boundary-respecting external module (e.g. the Native Work & Evidence
        spine) can run its read-modify-write / JSONL append under the same
        exclusive lock without reaching across the module boundary into a
        private helper. The yielded ``(previous, current)`` acquisition token is
        optional for callers and lets assurance fence an out-of-lock Git probe
        against current-client config transactions. An unlocked RMW is a
        lost-update fail-open.
        """
        return self._config_lock(timeout=timeout, poll=poll)

    def coverage_transaction_lock(self, *, timeout: float = 70.0,
                                  poll: float = 0.05):
        """Serialize one assurance coverage scan per root.

        Holding this lock across canonical-path preflight, command execution,
        and postflight prevents concurrent agenttalk scans from cross-claiming
        evidence. A separate coverage-only handoff lock orders this lock's
        release with final gate compare-and-swap without holding the global
        config lock across either subprocess work or lock release. The scan
        assumes every concurrent coverage producer implements that current
        handoff protocol; a legacy coverage-lock-only holder is not ordered
        after release. The scan never takes custody of root coverage reports:
        a preflight conflict causes refusal, and agenttalk does not read its
        contents, move it, or remove it. This lock does not filesystem-isolate
        the configured command. The underlying store lock supplies the same
        cross-platform ownership and stale-holder recovery as other store
        transactions.
        """
        assurance_dir = self._ensure_plain_assurance_lock_directory()
        return self._exclusive_lock(
            assurance_dir / "coverage.lock",
            timeout=timeout,
            poll=poll,
            what="assurance coverage transaction lock",
        )

    def coverage_handoff_lock(self, *, timeout: float = 70.0,
                              poll: float = 0.05):
        """Order coverage-lock release with the succeeding gate transaction.

        A holder takes this coverage-only lock once after acquiring
        ``coverage.lock`` to obtain its expected gate generation, and again
        while releasing ``coverage.lock`` and committing its final gate. The
        next coverage holder can acquire ``coverage.lock`` after that release,
        but cannot obtain its own expected generation or start its command
        until the prior handoff finishes, provided every producer implements
        this current two-lock protocol. Unlike ``config.lock``, this lock never
        blocks unrelated gate, waiver, roster, or configuration work.
        """
        assurance_dir = self._ensure_plain_assurance_lock_directory()
        return self._exclusive_lock(
            assurance_dir / "coverage-handoff.lock",
            timeout=timeout,
            poll=poll,
            what="assurance coverage gate handoff lock",
        )

    def _ensure_plain_assurance_lock_directory(self) -> Path:
        self._ensure_plain_lock_directory(
            self.dir,
            what="AgentTalk runtime lock directory",
        )
        assurance_dir = self.dir / "assurance"
        self._ensure_plain_lock_directory(
            assurance_dir,
            what="assurance lock directory",
        )
        return assurance_dir

    @staticmethod
    def _ensure_plain_lock_directory(path: Path, *, what: str) -> None:
        """Create or validate one lock parent without following a link."""
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
        except OSError as exc:
            raise OSError(f"could not create {what} at {path}: {exc}") from exc
        try:
            path_stat = os.lstat(path)
        except OSError as exc:
            raise OSError(f"could not inspect {what} at {path}: {exc}") from exc
        if not stat.S_ISDIR(path_stat.st_mode) or _is_reparse_point(path_stat):
            raise OSError(
                f"unsafe {what} at {path}: expected a plain directory, "
                "found a symlink, reparse point, or non-directory object"
            )

    def _supervisor_lifecycle_lock(self, *, timeout: float = 10.0,
                                   poll: float = 0.05):
        """Serialize supervisor claim/release, host selection, and refresh.

        When more than one supervisor lock is needed, the owning layer must
        acquire ``lifecycle -> PowerShell selection -> config`` in that order.
        These helpers are intentionally non-reentrant.
        """
        return self._exclusive_lock(
            self.dir / "supervisor-lifecycle.lock",
            timeout=timeout,
            poll=poll,
            what="supervisor lifecycle lock",
        )

    def _powershell_selection_lock(self, *, timeout: float = 10.0,
                                   poll: float = 0.05):
        """Serialize reads/writes that linearize PowerShell host use."""
        return self._exclusive_lock(
            self.dir / "powershell-host.lock",
            timeout=timeout,
            poll=poll,
            what="PowerShell host selection lock",
        )

    def _retirement_lock(self, *, timeout: float = 10.0, poll: float = 0.005):
        """Serialize roster retirement against final message publication.

        This is a persistent OS-lock inode, so sends pay no per-message marker
        creation, metadata fsync, or unlink cost. The durable payload is already
        prepared before this narrow critical section begins.
        """
        return self._lock_generation_guard(
            self.dir / "retirement",
            deadline=time.monotonic() + timeout,
            poll=poll,
            what="retirement/message publication",
        )

    def _message_publication_lock(
        self,
        *,
        timeout: float = 10.0,
        poll: float = 0.005,
    ):
        """Linearize every canonical message publication with dispatch replay."""
        return self._lock_generation_guard(
            self.dir / "message-publication",
            deadline=time.monotonic() + timeout,
            poll=poll,
            what="message publication",
        )

    @property
    def _message_publication_order_path(self) -> Path:
        return self.state_dir / "message-publication-order.json"

    @property
    def _message_publication_order_anchor_path(self) -> Path:
        return self.state_dir / "message-publication-order.anchor.json"

    @staticmethod
    def _message_publication_order_chain(
        messages: dict[str, int],
        *,
        through: int | None = None,
    ) -> str:
        limit = len(messages) if through is None else through
        by_sequence = {sequence: message_id for message_id, sequence in messages.items()}
        digest = hashlib.sha256(b"agenttalk-message-publication-order-v1").hexdigest()
        for sequence in range(1, limit + 1):
            message_id = by_sequence.get(sequence)
            if not isinstance(message_id, str):
                raise ValueError("message publication order sequence is incomplete")
            digest = hashlib.sha256(
                f"{digest}:{sequence}:{message_id}".encode("utf-8")
            ).hexdigest()
        return digest

    @classmethod
    def _validate_message_publication_order(cls, raw: object) -> dict:
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != _MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION
            or not isinstance(raw.get("append_sequence"), int)
            or isinstance(raw.get("append_sequence"), bool)
            or int(raw["append_sequence"]) < 0
            or not isinstance(raw.get("messages"), dict)
            or (
                "order_reconstructed" in raw
                and not isinstance(raw.get("order_reconstructed"), bool)
            )
        ):
            raise ValueError("message publication order sidecar is invalid")
        append_sequence = int(raw["append_sequence"])
        messages = raw["messages"]
        if any(
            not isinstance(message_id, str)
            or _ID_RE.fullmatch(message_id) is None
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            for message_id, sequence in messages.items()
        ):
            raise ValueError("message publication order entry is invalid")
        sequences = set(messages.values())
        if (
            len(messages) != append_sequence
            or sequences != set(range(1, append_sequence + 1))
        ):
            raise ValueError("message publication order sequence is non-contiguous")
        return raw

    @staticmethod
    def _validate_message_publication_order_anchor(raw: object) -> dict:
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != _MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION
            or not isinstance(raw.get("append_sequence"), int)
            or isinstance(raw.get("append_sequence"), bool)
            or int(raw["append_sequence"]) < 0
            or re.fullmatch(r"[0-9a-f]{64}", str(raw.get("chain_digest", "")))
            is None
        ):
            raise ValueError("message publication order anchor is invalid")
        return raw

    def _read_message_publication_order(self) -> dict | None:
        """Read + verify the publication-order sidecar (or None for a legacy store).

        Reads the ANCHOR before the sidecar. ``append_sequence`` is monotonic
        (append-only; the pinned prefix is never rewritten), so anchor-first makes
        ``anchor_seq <= sidecar_seq`` hold for any concurrent writer's two-file
        update — a LOCK-FREE reader can never observe the anchor ahead of the
        sidecar merely from a race (#37 HIGH: the old sidecar-first read raised a
        false ``rolled back`` tamper on every busy send, which silently degraded
        owed-action/terminal projection). A bounded re-read rides out the torn
        two-file window; only a DURABLE anchor-ahead-of-sidecar survives, and that
        means the sidecar lost committed history (genuine corruption) → fail loud.
        Under the publication lock (write path) there is no concurrent writer, so
        the first attempt is always consistent and the retry never sleeps.
        """
        order_path = self._message_publication_order_path
        anchor_path = self._message_publication_order_anchor_path
        attempts = 4
        for attempt in range(attempts):
            last = attempt == attempts - 1
            if not order_path.exists():
                if not anchor_path.exists():
                    return None
                # A lone anchor: transient (mid two-file write) or the sidecar was
                # lost/removed. Retry to clear the race, then fail loud with cause.
                if not last:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise ValueError(
                    "message publication order sidecar is missing while its anchor "
                    "exists (the durable order file was lost or removed; the anchor "
                    "cannot be satisfied) — investigate; do not delete the anchor "
                    "to work around this"
                )
            # Anchor FIRST (see docstring), then the sidecar.
            anchor = None
            if anchor_path.exists():
                try:
                    raw_anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "message publication order anchor is unreadable"
                    ) from exc
                anchor = self._validate_message_publication_order_anchor(raw_anchor)
            try:
                raw_order = json.loads(order_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "message publication order sidecar is unreadable"
                ) from exc
            order = self._validate_message_publication_order(raw_order)
            if anchor is None:
                # Sidecar present, anchor absent: contiguity-validated but not
                # tamper-pinned (a crash during first-bootstrap wrote the sidecar
                # at :1640 before the anchor at :1644, or the anchor was removed).
                # Serve it — failing loud would wedge a benign crash-recovery — and
                # let the next write re-anchor it (`_reserve...` always rewrites the
                # anchor). #37 MED fold.
                return order
            anchor_sequence = int(anchor["append_sequence"])
            order_sequence = int(order["append_sequence"])
            if anchor_sequence > order_sequence:
                # Anchor ahead of the sidecar: a torn lock-free read (retry clears
                # it) or, if durable, the sidecar lost committed history.
                if not last:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise ValueError(
                    "message publication order anchor is ahead of its sidecar "
                    "(the durable order lost committed history — corruption, not a "
                    "recoverable version-skew); investigate before writing"
                )
            prefix_digest = self._message_publication_order_chain(
                order["messages"],
                through=anchor_sequence,
            )
            if prefix_digest != anchor["chain_digest"]:
                raise ValueError(
                    "message publication order sidecar changed below its anchor "
                    "(chain digest mismatch — the pinned order was modified or "
                    "corrupted; this is NOT a recoverable version-skew) — "
                    "investigate before writing; do not delete the sidecar blindly"
                )
            return order
        return None  # unreachable: the loop returns or raises on the last attempt

    @classmethod
    def _extend_order_with_orphans(
        cls, order: dict, orphan_ids: list[str]
    ) -> dict:
        """Return a NEW order dict with ``orphan_ids`` appended at the tail in
        deterministic id-order — the SAME rule the initial bootstrap uses
        (`sorted(..., key=id)`). Existing entries are never rewritten, so the
        anchored prefix is preserved: healing can neither reorder nor mask
        committed history, only assign fresh tail sequences to messages already
        validly on disk (#37). Inter-orphan order is best-effort under cross-writer
        clock skew (ids are timestamp-prefixed); consumers of publication order
        must already tolerate out-of-causal-order delivery."""
        messages = dict(order["messages"])
        sequence = int(order["append_sequence"])
        for orphan_id in sorted(orphan_ids):
            if orphan_id in messages:
                continue
            sequence += 1
            messages[orphan_id] = sequence
        return {
            "schema_version": _MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION,
            "append_sequence": sequence,
            "messages": messages,
            # Once any tail was reconstructed from message ids, the sidecar can
            # preserve that canonical order but can never recover its original
            # physical publication order. Keep the provenance fail-closed across
            # every later write.
            "order_reconstructed": True,
        }

    def _reserve_message_publication_sequence(
        self,
        message_id: str,
        existing_messages: list[Message],
    ) -> int:
        """Durably reserve physical append order before publishing message bytes.

        Runs under the publication lock. Self-heals a version-skew store (an older
        or bypassing writer appended message files without an order entry) instead
        of wedging every send (#37); also re-anchors an anchor-absent sidecar,
        because the tail unconditionally rewrites both files below.
        """
        order = self._read_message_publication_order()
        if order is None:
            ordered_legacy = sorted(existing_messages, key=lambda message: message.id)
            messages = {
                message.id: sequence
                for sequence, message in enumerate(ordered_legacy, start=1)
            }
            order = {
                "schema_version": _MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION,
                "append_sequence": len(messages),
                "messages": messages,
                "order_reconstructed": bool(messages),
            }
        else:
            messages = dict(order["messages"])
            missing = [
                message.id for message in existing_messages if message.id not in messages
            ]
            if missing:
                # Version-skew / bypassed-writer self-heal: fold the orphans onto
                # the tail in id-order rather than raising on every send. The
                # anchored prefix is untouched (see _extend_order_with_orphans).
                order = self._extend_order_with_orphans(order, missing)
                messages = dict(order["messages"])
                logger.warning(
                    "agenttalk store: healed %d orphan message(s) with no "
                    "publication-order entry (cause undetermined: version skew or "
                    "a writer that bypassed _reserve_message_publication_sequence) "
                    "— upgrade/inspect all writers sharing this store; first=%s",
                    len(missing), sorted(missing)[0],
                )
        if message_id in messages:
            raise ValueError("message id already has a durable publication order")
        sequence = int(order["append_sequence"]) + 1
        messages[message_id] = sequence
        updated = {
            "schema_version": _MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION,
            "append_sequence": sequence,
            "messages": messages,
            # Missing provenance is a pre-fix/older-writer shape. It cannot prove
            # that this complete sidecar was never reconstructed, so upgrade it to
            # the permanent fail-closed state rather than silently blessing it.
            "order_reconstructed": order.get("order_reconstructed") is not False,
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self._message_publication_order_path,
            json.dumps(updated, indent=2, ensure_ascii=False),
        )
        _atomic_write_text(
            self._message_publication_order_anchor_path,
            json.dumps({
                "schema_version": _MESSAGE_PUBLICATION_ORDER_SCHEMA_VERSION,
                "append_sequence": sequence,
                "chain_digest": self._message_publication_order_chain(messages),
            }, indent=2, ensure_ascii=False),
        )
        return sequence

    @staticmethod
    def _cfg_dict(cfg: dict, key: str) -> dict:
        """Return cfg[key] as a dict, coercing absent/null to a fresh {}.

        ``load_config`` accepts an explicit ``"groups": null`` / ``"roles":
        null`` (treated as 'none defined'), but ``dict.setdefault`` would
        return that ``None`` and the next item assignment would raise
        ``TypeError``. Mutators go through here so a null-valued config is
        upgraded in place rather than crashing.
        """
        v = cfg.get(key)
        if not isinstance(v, dict):
            v = {}
            cfg[key] = v
        return v

    def groups(self) -> dict:
        """Return the ``{group: [members]}`` map ({} if none defined)."""
        return self.load_config().get("groups", {}) or {}

    def roles(self) -> dict:
        """Return the ``{agent: role}`` map ({} if none defined)."""
        return self.load_config().get("roles", {}) or {}

    def trust_classes(self) -> dict[str, str]:
        return self.load_config().get("trust_classes", {}) or {}

    def trust_class(self, name: str) -> str | None:
        value = self.trust_classes().get(name)
        return value if value in KNOWN_TRUST_CLASSES else None

    def avatar_preferences(self) -> dict[str, str]:
        """Return sanitized display-avatar preferences.

        Avatar preferences are deliberately fail-soft: bad hand-edited entries
        never brick config loading or rendering.
        """
        cfg = self.load_config()
        prefs, _warnings = _avatars.sanitize_avatar_preferences(
            cfg.get("avatars"), cfg.get("agents", []) or [])
        return prefs

    # ----------------------------------------------- identity registry (0.16.0)
    #
    # Two roster VIEWS that deliberately diverge (#19 Phase A, RFC §"Identity
    # Registry"). The ACTIVE roster (`agents`) is the set of sendable
    # identities; SEND and audience resolution use it. The KNOWN roster
    # (active ∪ retired tombstones) is what HISTORY validation uses, so a
    # message authored by a now-retired identity stays valid forever (FR-006,
    # immutable history) even though that identity can no longer send (FR-004).

    @staticmethod
    def _retired_names(cfg: dict) -> list[str]:
        out: list[str] = []
        for e in cfg.get("retired") or []:
            if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]:
                out.append(e["name"])
        return out

    @staticmethod
    def _known_roster(cfg: dict) -> list[str]:
        """Active ∪ retired, active first, de-duped (case-sensitively — the
        case-insensitive non-rebindable guard lives in the mutators)."""
        seen: set[str] = set()
        out: list[str] = []
        for n in list(cfg.get("agents", []) or []) + Store._retired_names(cfg):
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def active_agents(self) -> list[str]:
        """The sendable roster (``config['agents']``)."""
        return list(self.load_config().get("agents", []) or [])

    def retired_agents(self) -> list[str]:
        """Retired tombstone names (permanent, non-rebindable)."""
        return self._retired_names(self.load_config())

    def known_agents(self) -> list[str]:
        """Active ∪ retired — the roster HISTORY is validated against."""
        return self._known_roster(self.load_config())

    def resolve_audience(self, target: str, *, exclude: str | None = None) -> list[str]:
        """Resolve a broadcast target to a concrete recipient list.

        ``target`` is either ``"all"`` (the whole roster) or a defined
        group name. ``exclude`` drops one member (the sender). Raises
        ``ValueError`` for an unknown group so a typo can't silently
        broadcast to nobody.
        """
        cfg = self.load_config()
        roster = cfg.get("agents", []) or []
        if isinstance(target, str) and target.casefold() == "all":
            members = list(roster)
        else:
            groups = cfg.get("groups", {}) or {}
            if target not in groups:
                raise ValueError(
                    f"unknown group {target!r} (known: {sorted(groups)} + 'all')"
                )
            members = list(groups[target])
        # De-dupe (preserve order) and drop the sender.
        seen: set[str] = set()
        out: list[str] = []
        for m in members:
            if m != exclude and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def resolve_role_audience(self, role: str, *, exclude: str | None = None) -> list[str]:
        """Resolve a ROLE to its concrete member list (0.15.0, #15).

        A deliberate sibling of :meth:`resolve_audience`, not an overload:
        roles and groups are distinct config maps with distinct semantics,
        and overloading one resolver would create exactly the role/group
        name-collision ambiguity the spec forbids. Members are returned in
        roster order, de-duped, with ``exclude`` (the sender) dropped.
        Raises ``ValueError`` for an unknown role (naming the known ones)
        or an audience that is empty after exclusion — a typo must never
        silently broadcast to nobody.
        """
        cfg = self.load_config()
        roster = cfg.get("agents", []) or []
        roles = cfg.get("roles", {}) or {}
        known = sorted(set(roles.values()))
        if role not in known:
            raise ValueError(
                f"unknown role {role!r} (known roles: {known or '(none assigned)'})"
            )
        seen: set[str] = set()
        out: list[str] = []
        for a in roster:  # roster order, like groups
            if roles.get(a) == role and a != exclude and a not in seen:
                seen.add(a)
                out.append(a)
        if not out:
            raise ValueError(
                f"role {role!r} has no members besides {exclude!r} — "
                f"nobody would receive this broadcast"
            )
        return out

    def _assert_external_worker_authority_safe(self, cfg: dict) -> None:
        """Prevent accidental authority assignment at roster mutation time.

        This is deliberately the narrow watched-trial guard. Broader hostile-
        model enforcement at close/release time remains a production follow-up.
        """
        external = {
            agent
            for agent, value in (cfg.get("trust_classes") or {}).items()
            if value == TRUST_CLASS_EXTERNAL_WORKER
        }
        if not external:
            return
        roles = cfg.get("roles") or {}
        for agent in sorted(external):
            if isinstance(roles.get(agent), str) and roles[agent].casefold() == "lead":
                raise ValueError(
                    f"external-worker {agent!r} cannot hold the lead role"
                )
            if cfg.get("operator_facing") == agent:
                raise ValueError(
                    f"external-worker {agent!r} cannot be operator-facing"
                )

        from agenttalk import close as _close

        policy, error = _close.load_signoff_policy(self)
        if error:
            raise ValueError(
                "cannot prove external-worker signoff exclusion while "
                f"signoffs.json is invalid: {error}"
            )
        if policy is None:
            return
        default_reviewers = policy["defaults"]["reviewers"]
        all_domain_reviewers = _close.signoff_domain_refset(self, cfg, None)
        candidate_sets = [
            _close.resolve_signoff_candidates(
                cfg,
                candidate_refset=default_reviewers,
                default_reviewers={},
                use_default_reviewers=False,
                domain_refset={},
                include_domain_reviewers=False,
            )
        ]
        for sets in policy["risk_policies"].values():
            for signoff_set in sets:
                candidate_sets.append(
                    _close.resolve_signoff_candidates(
                        cfg,
                        candidate_refset=signoff_set["candidates"],
                        default_reviewers=default_reviewers,
                        use_default_reviewers=signoff_set["use_default_reviewers"],
                        domain_refset=all_domain_reviewers,
                        include_domain_reviewers=signoff_set["include_domain_reviewers"],
                    )
                )
        for agent in sorted(external):
            if any(agent in candidates for candidates in candidate_sets):
                raise ValueError(
                    f"external-worker {agent!r} cannot be a signoff candidate"
                )

    def add_agent(self, name: str, *, role: str | None = None,
                  groups: list[str] | None = None,
                  trust_class: str | None = None) -> dict:
        """Add an agent to the roster (idempotent) and optionally set its
        role / group memberships. A deliberate local admin op — NOT a
        security boundary and NOT process supervision."""
        validate_agent_name(name)
        with self._config_lock():
            cfg = self.load_config()
            roster = list(cfg.get("agents", []))
            is_new = name not in roster
            if is_new:
                # B2 (#19 Phase A): a retired tombstone is permanent and
                # non-rebindable (FR-002). Refuse at WRITE time — do not rely on
                # load_config fail-closing on the next read (that writes a bad
                # config first and gives a confusing error later). Case-insensitive,
                # because a tombstone must be unrepresentable as a new active name.
                retired_keys = {r.casefold(): r for r in self._retired_names(cfg)}
                if name.casefold() in retired_keys:
                    raise ValueError(
                        f"agent name {name!r} is a retired tombstone "
                        f"({retired_keys[name.casefold()]!r}) and cannot be re-bound; "
                        f"tombstones are permanent (#19). Pick a different name."
                    )
                validate_agent_roster(roster + [name])  # case-insensitive uniqueness
                roster.append(name)
                cfg["agents"] = roster
            if role is not None:
                roles = self._cfg_dict(cfg, "roles")
                # Route through the same choke point as set_role so `add --role
                # lead` can't bypass the at-most-one-lead invariant (review BLOCKING #1).
                self._assign_role_enforcing_lead(roles, name, role)
                validate_roles(roles, roster)
            if groups:
                g = self._cfg_dict(cfg, "groups")
                for gn in groups:
                    validate_group_name(gn)
                    members = g.setdefault(gn, [])
                    if name not in members:
                        members.append(name)
                validate_groups(g, roster)
            if trust_class is not None:
                trust_classes = self._cfg_dict(cfg, "trust_classes")
                trust_classes[name] = trust_class
                validate_trust_classes(trust_classes, roster)
            self._assert_external_worker_authority_safe(cfg)
            # All validation passed — only now perform side effects, so a bad
            # role/group never orphans a freshly-written cursor file.
            self._write_config(cfg)
            if is_new:
                cur = self.state_dir / f"{name}.cursor"
                if not cur.exists():
                    _atomic_write_text(cur, "")
        return cfg

    def remove_agent(self, name: str) -> dict:
        """Remove an agent from the roster, its role, and all group
        memberships. External workers become permanent tombstones; ordinary
        force-removed identities retain the historical re-addable behavior."""
        with self._retirement_lock(), self._config_lock():
            cfg = self.load_config()
            roster = list(cfg.get("agents", []))
            was_external = (
                name in roster
                and (cfg.get("trust_classes") or {}).get(name)
                == TRUST_CLASS_EXTERNAL_WORKER
            )
            if name in roster:
                roster.remove(name)
                cfg["agents"] = roster
            if isinstance(cfg.get("roles"), dict):
                cfg["roles"].pop(name, None)
            if isinstance(cfg.get("groups"), dict):
                for members in cfg["groups"].values():
                    if name in members:
                        members.remove(name)
            # Drop any managed_lead_loop entry so a dangling key can't brick load_config.
            if isinstance(cfg.get("managed_lead_loop"), dict):
                cfg["managed_lead_loop"].pop(name, None)
            if isinstance(cfg.get("avatars"), dict):
                cfg["avatars"].pop(name, None)
            if isinstance(cfg.get("trust_classes"), dict):
                cfg["trust_classes"].pop(name, None)
            if was_external:
                retired = cfg.get("retired")
                if not isinstance(retired, list):
                    retired = []
                retired.append({
                    "name": name,
                    "retired_at": _now_iso(),
                    "renamed_to": None,
                    "reason": "removed external-worker identity",
                })
                cfg["retired"] = retired
                validate_retired(retired, roster)
            self._write_config(cfg)
        return cfg

    def set_trust_class(self, name: str, trust_class: str | None) -> dict:
        with self._config_lock():
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
            if name not in roster:
                raise ValueError(f"agent {name!r} is not in the roster {sorted(roster)}")
            values = self._cfg_dict(cfg, "trust_classes")
            if (
                values.get(name) == TRUST_CLASS_EXTERNAL_WORKER
                and trust_class != TRUST_CLASS_EXTERNAL_WORKER
            ):
                raise ValueError(
                    f"external-worker trust for {name!r} cannot be cleared or reclassified; "
                    "retire the identity instead"
                )
            if trust_class is None:
                values.pop(name, None)
            else:
                values[name] = trust_class
            validate_trust_classes(values, roster)
            self._assert_external_worker_authority_safe(cfg)
            self._write_config(cfg)
        return cfg

    def set_role(self, name: str, role: str) -> list[str]:
        """Set ``name``'s role, enforcing an at-most-one-``lead`` invariant.

        If ``role`` is the lead role (compared case-insensitively, but stored
        verbatim) and other agents already hold it, they are demoted in the
        SAME config write — so the team can never end up with two leads, and
        switching the lead is one atomic op rather than a demote-then-promote
        two-step (0.24.0, feedback 3.1). Setting ``lead`` on the agent that is
        already the sole lead is an idempotent no-op.

        Returns the list of agents demoted from lead by this call (empty unless
        a lead was actually moved). The previous return value (the cfg dict) was
        unused by any caller. Zero leads remains a valid state — this never
        forces a lead to exist.
        """
        with self._config_lock():
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
            if name not in roster:
                raise ValueError(f"agent {name!r} is not in the roster {sorted(roster)}")
            roles = self._cfg_dict(cfg, "roles")
            demoted = self._assign_role_enforcing_lead(roles, name, role)
            validate_roles(roles, roster)
            self._assert_external_worker_authority_safe(cfg)
            self._write_config(cfg)
        return demoted

    @staticmethod
    def _assign_role_enforcing_lead(roles: dict, name: str, role: str) -> list[str]:
        """Set ``roles[name] = role``, enforcing the at-most-one-``lead``
        invariant: if ``role`` is the lead role (case-insensitive, stored
        verbatim), every OTHER current lead is demoted first. Returns the demoted
        agent names (normally 0 or 1; a hand-edited/legacy config with several
        leads is self-healed). The SINGLE choke point shared by every role-write
        path (``set_role`` and ``add_agent``) so the invariant can't be bypassed.
        Caller holds the config lock and runs ``validate_roles`` afterwards."""
        demoted: list[str] = []
        if role.casefold() == "lead":
            for other, r in list(roles.items()):
                if other != name and isinstance(r, str) and r.casefold() == "lead":
                    roles.pop(other, None)
                    demoted.append(other)
        roles[name] = role
        return demoted

    def sole_lead(self) -> str | None:
        """The single active agent whose role is ``lead`` (case-insensitive), or
        None. Returns None for ZERO leads AND for the legacy >1 case: ambiguity
        reads as "no unambiguous lead", so escalation falls through to its
        remediation path rather than guessing a target (0.24.0, research D3)."""
        cfg = self.load_config()
        roster = cfg.get("agents", []) or []
        roles = cfg.get("roles") or {}
        leads = [a for a in roster
                 if isinstance(roles.get(a), str) and roles[a].casefold() == "lead"]
        return leads[0] if len(leads) == 1 else None

    def protected_agents(self) -> set[str]:
        """Agents the supervisor must NEVER auto-kill/relaunch (WP-2): the
        ``operator_facing`` liaison UNION EVERY active ``role=lead`` agent.

        Fails CLOSED on ambiguity by design — unlike ``sole_lead`` (which
        collapses 2+ leads to None), this protects ALL leads, so a 2-lead team
        with no liaison still has both human channels protected from an
        unattended auto-restart. Read-only.
        """
        cfg = self.load_config()
        roster = cfg.get("agents", []) or []
        roles = cfg.get("roles") or {}
        protected = {a for a in roster
                     if isinstance(roles.get(a), str)
                     and roles[a].casefold() == "lead"}
        liaison = self.operator_facing()
        if liaison is not None:
            protected.add(liaison)
        return protected

    def set_group(self, group: str, members: list[str]) -> dict:
        with self._config_lock():
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
            validate_group_name(group)
            for m in members:
                if m not in roster:
                    raise ValueError(f"group member {m!r} is not in the roster {sorted(roster)}")
            groups = self._cfg_dict(cfg, "groups")
            groups[group] = list(dict.fromkeys(members))  # de-dupe, preserve order
            validate_groups(groups, roster)
            self._assert_external_worker_authority_safe(cfg)
            self._write_config(cfg)
        return cfg

    # ------------------------------------------------- operator liaison
    #
    # `operator_facing` is a single optional config slot naming the ONE
    # agent the human operator talks to directly (the liaison). It is
    # advisory ROUTING metadata, exactly like roles/groups: it never
    # affects message validity, thread closure, or authorization (see
    # SECURITY.md). Single-slot by representation — "two liaisons" is
    # unrepresentable rather than merely warned about. Added in 0.14.0
    # (issue #18). Tolerance follows the roles/groups precedent: an
    # absent / null / non-string / stale value reads as "not
    # configured" and never crashes a command.

    def operator_facing_raw(self) -> str | None:
        """The configured operator_facing value WITHOUT a roster check.

        Diagnostics (doctor) need to distinguish "not configured" from
        "configured but the agent is gone" — this returns whatever
        non-empty string the config holds, valid or not.
        """
        v = self.load_config().get("operator_facing")
        return v if isinstance(v, str) and v else None

    def operator_facing(self) -> str | None:
        """The designated liaison, or None when unset or not in the roster.

        Routing callers (`escalate`) use this: a stale designation must
        not route an operator question to a pruned mailbox.
        """
        cfg = self.load_config()
        v = cfg.get("operator_facing")
        if not (isinstance(v, str) and v):
            return None
        roster = cfg.get("agents", []) or []
        return v if v in roster else None

    def operator_identity_raw(self) -> str | None:
        """The configured operator bus principal without validation."""
        v = self.load_config().get("operator_identity")
        return v if isinstance(v, str) and v else None

    def operator_identity(self, *, lead: str | None = None) -> str:
        """The human operator's bus sender.

        This resolver intentionally has zero fallback. A dashboard-originated
        operator message must come from a dedicated reserved principal, never
        from the lead, liaison, environment, or browser payload.
        """
        value = self.operator_identity_raw()
        if value is None:
            raise ValueError(
                "operator_identity is not configured; set config.json "
                f"operator_identity to {_avatars.OPERATOR_PRINCIPAL!r}"
            )
        if value not in _avatars.RESERVED_PRINCIPALS:
            raise ValueError(
                f"operator_identity {value!r} is not a reserved bus principal "
                f"{sorted(_avatars.RESERVED_PRINCIPALS)}"
            )
        if value != _avatars.OPERATOR_PRINCIPAL:
            raise ValueError(
                f"operator_identity {value!r} is not the supported operator "
                f"principal {_avatars.OPERATOR_PRINCIPAL!r}"
            )
        if lead is not None and value == lead:
            raise ValueError("operator_identity must not equal the lead identity")
        return value

    def lead_chat_lead(self) -> str:
        """Resolve the lead recipient for the dashboard lead-chat surface."""
        lead = self.operator_facing() or self.sole_lead()
        if lead is None:
            raise ValueError(
                "lead chat needs an operator-facing lead or exactly one role=lead agent"
            )
        return lead

    def lead_chat_identities(self) -> tuple[str, str]:
        """Return ``(operator, lead)`` for lead-chat with no identity fallback."""
        lead = self.lead_chat_lead()
        operator = self.operator_identity(lead=lead)
        return operator, lead

    def lead_chat_request_id(
        self, *, operator: str | None = None, lead: str | None = None
    ) -> str:
        """Stable chat thread id: ``lc-<sha256(session_id, operator, lead)>``."""
        if operator is None or lead is None:
            operator, lead = self.lead_chat_identities()
        session_id = self.load_config().get("session_id")
        validate_session_id(session_id)
        h = hashlib.sha256()
        for part in (session_id, operator, lead):
            data = str(part).encode("utf-8")
            h.update(len(data).to_bytes(4, "big"))
            h.update(data)
        return "lc-" + h.hexdigest()

    @staticmethod
    def _lead_chat_wrapped(health: dict) -> bool:
        mode = health.get("mode") if isinstance(health, dict) else None
        if not isinstance(mode, str):
            return False
        normalized = mode.casefold()
        return normalized.startswith("wrapper") or normalized == "lead-loop"

    def lead_chat_liveness(
        self, *, lead: str | None = None, now_epoch: float | None = None,
        heartbeat_stale_after: float = _LEAD_CHAT_HEARTBEAT_STALE_AFTER_SECONDS,
    ) -> dict:
        """Fail-closed availability for the dashboard lead-chat send path."""
        try:
            resolved_lead = lead or self.lead_chat_lead()
        except ValueError as e:
            return {
                "available": False,
                "status": "unavailable",
                "code": "lead_unavailable",
                "detail": str(e),
            }
        hb = self.read_heartbeat(resolved_lead)
        now = time.time() if now_epoch is None else float(now_epoch)
        stale_after = (
            float(heartbeat_stale_after)
            if isinstance(heartbeat_stale_after, (int, float))
            and heartbeat_stale_after >= 0
            else _LEAD_CHAT_HEARTBEAT_STALE_AFTER_SECONDS
        )
        if hb is None:
            return {
                "available": False,
                "status": "unavailable",
                "code": "lead_unavailable",
                "lead": resolved_lead,
                "reason": "lead heartbeat is missing",
                "heartbeat_age_seconds": None,
                "heartbeat_stale_after_seconds": stale_after,
            }
        heartbeat_age = self._bounded_heartbeat_age_seconds(hb, now)
        if heartbeat_age is None:
            return {
                "available": False,
                "status": "unavailable",
                "code": "lead_unavailable",
                "lead": resolved_lead,
                "reason": "lead heartbeat is outside allowed clock skew",
                "heartbeat_age_seconds": None,
                "heartbeat_stale_after_seconds": stale_after,
            }
        if heartbeat_age > stale_after:
            return {
                "available": False,
                "status": "unavailable",
                "code": "lead_unavailable",
                "lead": resolved_lead,
                "reason": "lead heartbeat is stale",
                "heartbeat_age_seconds": round(heartbeat_age, 3),
                "heartbeat_stale_after_seconds": stale_after,
            }
        health = self.read_health(resolved_lead, now_epoch=now, heartbeat=hb)
        state = health.get("state") if isinstance(health, dict) else _health.STATE_UNKNOWN
        reason = health.get("reason_code") if isinstance(health, dict) else None
        wrapped = self._lead_chat_wrapped(health)
        age = health.get("age_seconds") if isinstance(health, dict) else None
        if not wrapped:
            try:
                lead_chat_lead = self.lead_chat_lead()
            except ValueError:
                lead_chat_lead = None
            if resolved_lead == lead_chat_lead:
                status = {
                    _health.STATE_IDLE_WAITING: "idle",
                    _health.STATE_WORKING_TURN: "live",
                    _health.STATE_WORKING_SILENT: "away",
                }.get(state, "idle")
                live_reason = (
                    "lead heartbeat is fresh"
                    if reason in {None, "health_missing"}
                    else reason
                )
                return {
                    "available": True,
                    "status": status,
                    "code": "unwrapped_live",
                    "lead": resolved_lead,
                    "state": state,
                    "reason": live_reason,
                    "age_seconds": age,
                    "heartbeat_age_seconds": round(heartbeat_age, 3),
                    "heartbeat_stale_after_seconds": stale_after,
                    "health": health,
                }
            return {
                "available": False,
                "status": "unavailable",
                "code": "lead_unwrapped",
                "lead": resolved_lead,
                "state": state,
                "reason": reason or "lead is not a wrapped/managed process",
                "heartbeat_age_seconds": round(heartbeat_age, 3),
                "heartbeat_stale_after_seconds": stale_after,
                "health": health,
            }
        if state not in _LEAD_CHAT_AVAILABLE_STATES:
            return {
                "available": False,
                "status": "unavailable",
                "code": "lead_unavailable",
                "lead": resolved_lead,
                "state": state,
                "reason": reason or "lead is not currently available",
                "heartbeat_age_seconds": round(heartbeat_age, 3),
                "heartbeat_stale_after_seconds": stale_after,
                "health": health,
            }
        status = {
            _health.STATE_IDLE_WAITING: "idle",
            _health.STATE_WORKING_TURN: "live",
            _health.STATE_WORKING_SILENT: "away",
        }.get(state, "unavailable")
        return {
            "available": True,
            "status": status,
            "code": status,
            "lead": resolved_lead,
            "state": state,
            "age_seconds": age,
            "heartbeat_age_seconds": round(heartbeat_age, 3),
            "heartbeat_stale_after_seconds": stale_after,
            "health": health,
        }

    def is_release_authorized(self, sender: str) -> bool:
        """DEPRECATED legacy alias - delegates to the SINGLE loop-exit resolver
        :meth:`loop_exit_relay_authorized` (0.40.0 unification). It used to carry a
        divergent zero-lead any-active fallback, which made the CLI ``release``
        authority MORE permissive than the wrapper loop-exit classifier (an authority
        DRIFT the fresh audit flagged). There is now ONE resolver: no liaison + no
        sole lead -> FAIL CLOSED. Kept only so existing callers/tests keep one name."""
        return self.loop_exit_relay_authorized(sender)

    def loop_exit_relay_authorized(self, sender: str) -> bool:
        """The SINGLE resolver for who may relay a loop-EXIT control (release/end)
        that a listener obeys - used by both the wrapper loop-exit classifier and the
        CLI ``release`` command (0.40.0 unification; :meth:`is_release_authorized` is a
        thin delegating alias). Authority (stand-down authority, 0.39.0): the
        ``operator_facing`` liaison if set, ELSE the sole ``role=lead``, ELSE FAIL
        CLOSED. There is NO zero-lead any-active fallback - taking an agent offline is
        a human-relayed act, so an un-configured team must designate a liaison or a
        single lead (doctor/docs say so). Distinct from :meth:`protected_agents` (kill-
        protection, deliberately broad) - loop-exit authority is a different, narrower
        concern."""
        liaison = self.operator_facing()
        if liaison is not None:
            return sender == liaison
        lead = self.sole_lead()
        if lead is not None:
            return sender == lead
        return False  # no liaison + no sole lead -> no one may stand a listener down

    def set_operator_facing(self, name: str | None) -> dict:
        """Set (or clear, with None) the operator-facing designation.

        Validates roster membership at set time; reading tolerates a
        later roster change (see `operator_facing`).
        """
        with self._config_lock():
            cfg = self.load_config()
            if name is None:
                cfg.pop("operator_facing", None)
            else:
                roster = cfg.get("agents", []) or []
                if name not in roster:
                    raise ValueError(
                        f"agent {name!r} is not in the roster {sorted(roster)}"
                    )
                cfg["operator_facing"] = name
            self._assert_external_worker_authority_safe(cfg)
            self._write_config(cfg)
        return cfg

    # ----------------------------------------- retirement / rename (0.16.0 #19)
    #
    # Retire = move an active identity to a PERMANENT tombstone. It can no
    # longer send (FR-004), its name can never be re-bound (FR-002), but its
    # historical messages stay valid (FR-006, validated against the KNOWN
    # roster). Rename = retire(old -> new) + add(new), carrying over old's
    # role / groups / liaison bit. Every op touches ONLY config.json — history
    # is immutable (no message file is ever edited).

    @staticmethod
    def _strip_identity(cfg: dict, name: str) -> None:
        """Remove ``name`` from roster-adjacent config maps in place.

        Shared by retire/rename. Never touches message files.
        """
        roster = list(cfg.get("agents", []) or [])
        if name in roster:
            roster.remove(name)
            cfg["agents"] = roster
        if isinstance(cfg.get("roles"), dict):
            cfg["roles"].pop(name, None)
        if isinstance(cfg.get("groups"), dict):
            for members in cfg["groups"].values():
                if name in members:
                    members.remove(name)
        if cfg.get("operator_facing") == name:
            cfg.pop("operator_facing", None)
        # Drop the managed_lead_loop entry too: a left-behind key with no roster
        # member fails validate_managed_lead_loop -> bricks load_config (reviewer P1).
        m = cfg.get("managed_lead_loop")
        if isinstance(m, dict):
            m.pop(name, None)
        avatars = cfg.get("avatars")
        if isinstance(avatars, dict):
            avatars.pop(name, None)
        trust_classes = cfg.get("trust_classes")
        if isinstance(trust_classes, dict):
            trust_classes.pop(name, None)

    def set_avatar(self, name: str, avatar_id: str) -> dict:
        """Set an active roster member's display-avatar preference."""
        validate_agent_name(name)
        normalized = _avatars.normalize_avatar_id(avatar_id)
        if normalized is None:
            raise ValueError(
                f"unknown avatar id {avatar_id!r} "
                f"(known: {sorted(_avatars.AVATAR_ASSETS)})"
            )
        with self._config_lock():
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
            if name not in roster:
                raise ValueError(f"agent {name!r} is not in the roster {sorted(roster)}")
            avatars = self._cfg_dict(cfg, "avatars")
            avatars[name] = normalized
            self._write_config(cfg)
        return cfg

    def clear_avatar(self, name: str) -> dict:
        """Clear an active roster member's display-avatar preference."""
        validate_agent_name(name)
        with self._config_lock():
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
            if name not in roster:
                raise ValueError(f"agent {name!r} is not in the roster {sorted(roster)}")
            avatars = cfg.get("avatars")
            if isinstance(avatars, dict):
                avatars.pop(name, None)
                if not avatars:
                    cfg.pop("avatars", None)
            self._write_config(cfg)
        return cfg

    def set_operator_avatar(self, avatar_id: str) -> dict:
        """Set the reserved operator principal's display-avatar preference."""
        normalized = _avatars.normalize_avatar_id(avatar_id)
        if normalized is None:
            raise ValueError(
                f"unknown avatar id {avatar_id!r} "
                f"(known: {sorted(_avatars.AVATAR_ASSETS)})"
            )
        with self._config_lock():
            cfg = self.load_config()
            avatars = self._cfg_dict(cfg, "avatars")
            avatars[_avatars.OPERATOR_PRINCIPAL] = normalized
            self._write_config(cfg)
        return cfg

    def clear_operator_avatar(self) -> dict:
        """Clear the reserved operator principal's display-avatar preference."""
        with self._config_lock():
            cfg = self.load_config()
            avatars = cfg.get("avatars")
            if isinstance(avatars, dict):
                avatars.pop(_avatars.OPERATOR_PRINCIPAL, None)
                if not avatars:
                    cfg.pop("avatars", None)
            self._write_config(cfg)
        return cfg

    def retire_agent(self, name: str, *, reason: str | None = None,
                     renamed_to: str | None = None) -> dict:
        """Retire an active identity to a permanent tombstone (FR-002/003/004).

        ``renamed_to`` links a rename's tombstone to its successor (set by
        :meth:`rename_agent`). Refuses a name that is not currently active.
        """
        with self._retirement_lock(), self._config_lock():
            cfg = self.load_config()
            active = cfg.get("agents", []) or []
            if name not in active:
                if name in self._retired_names(cfg):
                    raise ValueError(f"identity {name!r} is already retired")
                raise ValueError(
                    f"cannot retire {name!r}: not in the active roster {sorted(active)}"
                )
            self._strip_identity(cfg, name)
            retired = cfg.get("retired")
            if not isinstance(retired, list):
                retired = []
            retired.append({
                "name": name,
                "retired_at": _now_iso(),
                "renamed_to": renamed_to,
                "reason": reason,
            })
            cfg["retired"] = retired
            validate_retired(retired, cfg.get("agents", []) or [])  # fail before write
            self._write_config(cfg)
        return cfg

    def rename_agent(self, old: str, new: str, *, reason: str | None = None) -> dict:
        """Safe rename = retire ``old`` (tombstone, ``renamed_to=new``) + add
        ``new`` as a new active identity, carrying over ``old``'s role, group
        memberships, and operator_facing bit. One atomic config write. History
        referencing ``old`` stays valid; ``old`` is non-rebindable (FR-002/005/006).
        """
        validate_agent_name(new)
        with self._retirement_lock(), self._config_lock():
            cfg = self.load_config()
            active = cfg.get("agents", []) or []
            if old not in active:
                raise ValueError(
                    f"cannot rename {old!r}: not in the active roster {sorted(active)}"
                )
            # Non-rebindable: `new` must not collide (case-insensitively) with ANY
            # known identity — active or a retired tombstone.
            known_keys = {k.casefold(): k for k in self._known_roster(cfg)}
            if new.casefold() in known_keys:
                clash = known_keys[new.casefold()]
                is_tomb = new.casefold() in {r.casefold() for r in self._retired_names(cfg)}
                where = "a retired tombstone" if is_tomb else "already an active identity"
                raise ValueError(
                    f"cannot rename {old!r} to {new!r}: {clash!r} is {where}; "
                    f"identities are non-rebindable (#19)"
                )
            # Snapshot old's role / group memberships / liaison BEFORE stripping.
            old_role = (cfg.get("roles") or {}).get(old)
            old_groups = [g for g, members in (cfg.get("groups") or {}).items()
                          if old in members]
            was_liaison = cfg.get("operator_facing") == old
            old_managed = (cfg.get("managed_lead_loop") or {}).get(old)
            old_trust_class = (cfg.get("trust_classes") or {}).get(old)
            avatars = cfg.get("avatars")
            old_avatar = avatars.get(old) if isinstance(avatars, dict) else None
            # Retire old -> tombstone(renamed_to=new), then activate new + carryover.
            self._strip_identity(cfg, old)
            retired = cfg.get("retired")
            if not isinstance(retired, list):
                retired = []
            retired.append({
                "name": old,
                "retired_at": _now_iso(),
                "renamed_to": new,
                "reason": reason,
            })
            cfg["retired"] = retired
            roster = list(cfg.get("agents", []) or [])
            roster.append(new)
            cfg["agents"] = roster
            if old_role is not None:
                self._cfg_dict(cfg, "roles")[new] = old_role
            if old_groups:
                g = self._cfg_dict(cfg, "groups")
                for gn in old_groups:
                    members = g.setdefault(gn, [])
                    if new not in members:
                        members.append(new)
            if was_liaison:
                cfg["operator_facing"] = new
            # Carry the managed_lead_loop spec onto `new` (parity with role/group/
            # liaison). _strip_identity already popped `old`'s key; without this the
            # rename would SILENTLY DROP the managed flag.
            if old_managed is not None:
                self._cfg_dict(cfg, "managed_lead_loop")[new] = old_managed
            if old_trust_class is not None:
                self._cfg_dict(cfg, "trust_classes")[new] = old_trust_class
            avatar_id = _avatars.normalize_avatar_id(old_avatar)
            if avatar_id is not None:
                self._cfg_dict(cfg, "avatars")[new] = avatar_id
            # Validate the WHOLE resulting config before writing (fail-closed).
            validate_agent_roster(roster)
            validate_retired(retired, roster)
            if cfg.get("roles"):
                validate_roles(cfg["roles"], roster)
            if cfg.get("groups"):
                validate_groups(cfg["groups"], roster)
            if cfg.get("managed_lead_loop"):
                validate_managed_lead_loop(cfg["managed_lead_loop"], roster)
            if cfg.get("trust_classes"):
                validate_trust_classes(cfg["trust_classes"], roster)
            self._assert_external_worker_authority_safe(cfg)
            self._write_config(cfg)
            cur = self.state_dir / f"{new}.cursor"
            if not cur.exists():
                _atomic_write_text(cur, "")
        return cfg

    def _drain_check(self, name: str) -> list[dict]:
        """Open (non-terminal) threads still owing work to/from ``name``.

        Pure query used by ``roster rename --drain-check``. ``threads`` imports
        ``store``, so import it lazily to avoid a cycle. Uses ``name``'s real
        cursor + ack-closed set so an already-acked thread does not block a
        rename. Returns thread row dicts (empty ⇒ safe to rename).
        """
        from agenttalk import threads as _threads  # lazy: avoid import cycle
        ts = self.read_threadstate(name)
        closed = {rid for rid, e in ts.items()
                  if isinstance(e, dict) and e.get("closed") is True}
        rows = _threads.derive_threads(
            self.valid_messages(), agent=name,
            cursor=self.cursor(name), closed_rids=closed,
        )
        owed: list[dict] = []
        for t in rows:
            if t.state in ("closed", "closed-superseded"):
                continue
            owed.append(t.to_dict())
        return owed

    def _open_thread_for(self, agent: str, request_id: str):
        """The non-terminal thread row ``request_id`` for ``agent``, or None.

        Returns None if the thread is unknown, not involving ``agent``, or
        already terminal (closed / closed-superseded). Used to gate forwarding
        on a genuinely *owed/open* obligation (lazy threads import — cycle)."""
        from agenttalk import threads as _threads
        ts = self.read_threadstate(agent)
        closed = {rid for rid, e in ts.items()
                  if isinstance(e, dict) and e.get("closed") is True}
        rows = _threads.derive_threads(
            self.valid_messages(), agent=agent,
            cursor=self.cursor(agent), closed_rids=closed,
        )
        for t in rows:
            if t.request_id == request_id:
                return None if t.state in ("closed", "closed-superseded") else t
        return None

    def _already_forwarded(self, request_id: str) -> bool:
        """True if any valid message already forwarded ``request_id`` (the
        forward note carries ``meta.forwarded_request_id``). Enforces single
        hop: a request can be forwarded at most once."""
        for m in self.valid_messages():
            if (m.meta or {}).get("forwarded_request_id") == request_id:
                return True
        return False

    def forward_retired(self, retired_name: str, to_agent: str, request_id: str,
                        *, from_agent: str | None = None,
                        reason: str | None = None) -> "Message":
        """Forward a SPECIFIC owed/open request from a retired identity to a
        live agent — one explicit hop (FR-008, B4). Emits an ordinary ``note``
        to ``to_agent`` carrying ``meta.forwarded_from`` +
        ``meta.forwarded_request_id``. Sender is ``from_agent`` (active) or the
        operator_facing identity — NEVER ``to_agent`` by default. Refuses a
        non-retired source, a non-active target, a request that is not a
        currently-open thread owed to/from the retired identity, a missing
        sender, or a second forward of the same request.
        """
        cfg = self.load_config()
        if retired_name not in self._retired_names(cfg):
            raise ValueError(
                f"cannot forward from {retired_name!r}: it is not a retired "
                f"identity (only retired tombstones can be forwarded)"
            )
        active = cfg.get("agents", []) or []
        if to_agent not in active:
            raise ValueError(
                f"cannot forward to {to_agent!r}: not in the active roster {sorted(active)}"
            )
        liaison = cfg.get("operator_facing")
        sender = from_agent or (liaison if liaison in active else None)
        if not sender:
            raise ValueError(
                "retired forwarding needs an explicit --from (active) sender; "
                "no operator_facing identity is set to default to"
            )
        if sender not in active:
            raise ValueError(
                f"forward sender {sender!r} is not an active identity {sorted(active)}"
            )
        if sender == to_agent:
            raise ValueError(
                "forward sender must not be the target (a forward must not look "
                "like it came from the agent receiving it)"
            )
        # Single hop: a request may be forwarded at most once (Codex WP01 B2).
        if self._already_forwarded(request_id):
            raise ValueError(
                f"request {request_id!r} was already forwarded; second hop forbidden"
            )
        # Must be a CURRENTLY-OPEN thread owed to/from the retired identity — a
        # closed/answered request has no obligation to forward (Codex WP01 B1).
        if self._open_thread_for(retired_name, request_id) is None:
            raise ValueError(
                f"request {request_id!r} is not an open thread owed to/from "
                f"{retired_name!r} — nothing to forward"
            )
        body = reason or (
            f"{retired_name} is retired; forwarding request {request_id} "
            f"to {to_agent}."
        )
        return self.send(
            sender=sender, recipient=to_agent, kind="note",
            subject=f"forwarded from {retired_name}",
            body=body,
            meta={
                "forwarded_from": retired_name,
                "forwarded_request_id": request_id,
                "forward": {"hop": 1},
            },
        )

    # --------------------------------------------------------------- writing

    def project_id(self) -> str:
        """Path-derived project identifier (not stored in config).

        Always returns a value (depends only on ``self.root``, never
        on anything inside ``.agenttalk/``). See
        ``signing.project_id_for_root`` for why this isn't UUID-in-
        config.json anymore.
        """
        return _signing.project_id_for_root(self.root)

    def signing_enforced(self) -> bool:
        """True iff HMAC signatures are enforced for this project.

        Anchored to the EXISTENCE of the per-user key file at the
        PATH-DERIVED ``project_id``. Both the project_id and the key
        file's presence are decided OUTSIDE attacker-writable
        ``.agenttalk/``: the ID is derived from ``self.root`` (which
        ``find_root()`` resolves before the bus even looks at
        config), and the key file lives under the per-user keys dir.

        Closes both v0.6.0 iter-1 (config flag bypass) and iter-2
        (config-stored project_id bypass).
        """
        try:
            return _signing.resolve_key_path(self.project_id()).exists()
        except (OSError, ValueError):
            return False

    # Legacy: 0.6.0-iter-1 wrote a ``require_signatures`` field AND
    # a ``project_id`` field in config.json. Both are ignored by
    # the verify path now (they're inside attacker-writable state).
    # ``agenttalk status`` surfaces a NOTE so users with upgraded
    # configs see the fields have no effect.
    def legacy_require_signatures_flag(self) -> bool | None:
        try:
            cfg = self.load_config()
        except (ValueError, OSError, FileNotFoundError):
            return None
        if "require_signatures" not in cfg:
            return None
        return bool(cfg["require_signatures"])

    def legacy_config_project_id(self) -> str | None:
        """Returns the (deprecated, ignored) project_id from
        config.json if a 0.6.0-iter-1 config wrote one. The verify
        path no longer consults this field; ``status`` surfaces it
        so users of upgraded configs see it's not load-bearing."""
        try:
            cfg = self.load_config()
        except (ValueError, OSError, FileNotFoundError):
            return None
        return cfg.get("project_id")

    def _validate_send_principals(
        self,
        cfg: dict,
        *,
        sender: str,
        recipient: str,
        allow_reserved_sender: bool,
    ) -> None:
        agents = set(cfg.get("agents", []))
        principals = _bus_principals(list(agents))
        retired = set(self._retired_names(cfg))
        if sender in _avatars.RESERVED_PRINCIPALS and not allow_reserved_sender:
            raise ValueError(
                f"sender '{sender}' is a reserved bus principal and can only be "
                "used by the lead-chat/operator-answer authority path"
            )
        sender_allowed = sender in agents or (
            allow_reserved_sender and sender in _avatars.RESERVED_PRINCIPALS
        )
        if agents and not sender_allowed:
            if sender in retired:
                raise ValueError(
                    f"sender '{sender}' is retired (a tombstone) and cannot "
                    "send; tombstones are permanent (#19). See `agenttalk roster`."
                )
            raise ValueError(
                f"sender '{sender}' not in registered bus principals {sorted(principals)}"
            )
        if agents and recipient not in principals:
            if recipient in retired:
                raise ValueError(
                    f"recipient '{recipient}' is retired (a tombstone) and cannot "
                    "receive new messages (#19). See `agenttalk roster`."
                )
            raise ValueError(
                f"recipient '{recipient}' not in registered bus principals {sorted(principals)}"
            )

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        body: str,
        kind: str = "message",
        subject: str = "",
        meta: dict | None = None,
        sign: bool | None = None,
        _allow_reserved_sender: bool = False,
        _config_locked: bool = False,
    ) -> Message:
        if not self.initialized():
            raise FileNotFoundError("agenttalk not initialized; run `agenttalk init`.")
        supplied_meta = dict(meta or {})
        authority_sensitive = bool(
            supplied_meta.get("origin_request_id")
            and supplied_meta.get("origin_inbound_id")
        )
        if authority_sensitive and not _config_locked:
            with self._config_lock():
                return self.send(
                    sender=sender,
                    recipient=recipient,
                    body=body,
                    kind=kind,
                    subject=subject,
                    meta=supplied_meta,
                    sign=sign,
                    _allow_reserved_sender=_allow_reserved_sender,
                    _config_locked=True,
                )
        config_before = os.stat(self.config_path)
        cfg = self.load_config()
        config_after = os.stat(self.config_path)
        config_revision = (
            _file_revision(config_after)
            if _file_revision(config_before) == _file_revision(config_after)
            else None
        )
        self._validate_send_principals(
            cfg,
            sender=sender,
            recipient=recipient,
            allow_reserved_sender=_allow_reserved_sender,
        )
        # Reject unknown kinds at WRITE time so the sender sees an
        # immediate error rather than a silent receive-side skip.
        # Without this, `agenttalk send --kind typo` would exit 0 +
        # the message would be invisible to the peer's wait/recv.
        if kind not in KNOWN_KINDS:
            raise ValueError(
                f"unknown kind {kind!r} (allowed: {sorted(KNOWN_KINDS)})"
            )
        # Epoch stamping (#19 Phase A): a tracked opener automatically records
        # the global epoch at send time. Three-state: an epoch-aware client
        # ALWAYS writes the key (barrier id, or null when no barrier has fired
        # yet); a pre-0.16.0 client never ran this code, so the key is absent.
        # A caller that already supplied `epoch_at_send` wins (broadcast
        # snapshots one epoch for the whole fan-out — B3).
        meta = supplied_meta
        if authority_sensitive:
            roster = list(cfg.get("agents") or [])
            roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
            authorized = {
                name for name in roster
                if isinstance(roles.get(name), str)
                and roles[name].casefold() == "lead"
            }
            liaison = cfg.get("operator_facing")
            if isinstance(liaison, str) and liaison in roster:
                authorized.add(liaison)
            roster_payload = {
                "agents": roster,
                "roles": {name: roles[name] for name in sorted(roles)},
                "operator_facing": liaison if isinstance(liaison, str) else None,
            }
            roster_revision = hashlib.sha256(
                json.dumps(
                    roster_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            expected_revision = meta.get("expected_roster_revision")
            if not isinstance(expected_revision, str) or not expected_revision:
                raise ValueError(
                    "origin-correlated authorized transition requires an expected "
                    "roster revision"
                )
            if expected_revision != roster_revision:
                raise ValueError("roster revision changed before authorized transition append")
            if recipient not in authorized:
                raise ValueError(
                    "origin-correlated escalation target is not an event-time "
                    "authorized liaison or lead"
                )
            meta["roster_revision"] = roster_revision
            meta["authorized_liaisons"] = sorted(authorized)
        _gates.validate_response_status(kind, meta)
        if kind in OPENER_KINDS and "epoch_at_send" not in meta:
            meta["epoch_at_send"] = self.current_epoch()
        msg = Message(
            id=_new_id(),
            ts=_now_iso(),
            sender=sender,
            recipient=recipient,
            kind=kind,
            subject=subject,
            body=body,
            meta=meta or {},
        )
        # Resolve signing policy: explicit kwarg > "key file exists"
        # rule. Default (sign=None + no key file) = no signature.
        if sign is None:
            sign = self.signing_enforced()
        if sign:
            project_id = self.project_id()
            try:
                key = _signing.load_key(project_id)
            except FileNotFoundError as e:
                raise ValueError(
                    f"cannot sign: {e}. Run `agenttalk hmac-init`."
                ) from e
            signed_dict = _signing.sign_message(
                msg.to_dict(), key, key_id=project_id,
            )
            msg = Message.from_dict(signed_dict)
        path = self.messages_dir / f"{msg.id}.json"
        payload = json.dumps(msg.to_dict(), indent=2, ensure_ascii=False)
        pending = self.messages_dir / f".{msg.id}.{uuid.uuid4().hex}.pending"
        pending_identity = _write_text_exclusive(pending, payload)
        try:
            lock = (
                contextlib.nullcontext()
                if _config_locked
                else self._retirement_lock()
            )
            with lock, self._message_publication_lock():
                # Durable payload preparation is deliberately OUTSIDE this lock,
                # so unrelated sends do not serialize their write+fsync cost.
                # Retirement and dispatch replay share persistent, narrow
                # mutexes with final publication. Revalidate immediately before
                # the O(1) commit.
                current_revision = _file_revision(os.stat(self.config_path))
                if config_revision is None or current_revision != config_revision:
                    cfg = self.load_config()
                self._validate_send_principals(
                    cfg,
                    sender=sender,
                    recipient=recipient,
                    allow_reserved_sender=_allow_reserved_sender,
                )
                self._reserve_message_publication_sequence(
                    msg.id,
                    self.valid_messages(),
                )
                try:
                    os.replace(pending, path)
                    _fsync_directory(path.parent)
                except PermissionError:
                    # Codex's Windows sandbox can hold process-lifetime handles
                    # that block rename. Preserve the established direct-write
                    # fallback there; ordinary Windows/POSIX stays pre-staged.
                    _atomic_write_text(path, payload)
                # Keep publication order and the monotonic owed-action projection
                # under one ordering mutex.  A delayed eager hook must not let a
                # later canonical message acquire an earlier reducer sequence.
                try:
                    from agenttalk.wrapper.obligations import note_bus_message

                    note_bus_message(self, msg)
                except (OSError, ValueError, RuntimeError):
                    # The immutable message is authoritative. A later replay repairs
                    # a missed projection in canonical publication order.
                    pass
        finally:
            try:
                _unlink_if_same_file(pending, pending_identity)
            except OSError:
                pass
        request_id = (msg.meta or {}).get("request_id")
        if isinstance(request_id, str) and request_id and kind not in CONTROL_KINDS:
            # Observational cleanup only. The validated thread remains the
            # authority, so a cleanup failure can neither lose the message nor
            # leave a false active wait edge.
            try:
                from agenttalk.coordination_stall import clear_terminal_awaits

                clear_terminal_awaits(self, request_id)
            except Exception as exc:  # noqa: BLE001 - observational cleanup only
                _ = exc
        return msg

    def _operation_intent_path(self, sender: str, operation_nonce: str) -> Path:
        name = validate_agent_name(sender)
        if re.fullmatch(r"[0-9a-f]{32}", operation_nonce) is None:
            raise ValueError("operation nonce must be exactly 32 lowercase hexadecimal characters")
        return self.state_dir / "operation-intents" / f"{name}.{operation_nonce}.json"

    @staticmethod
    def _operation_intent_identity(
        *,
        operation: str,
        kind: str,
        recipient: str,
        meta: dict,
    ) -> dict:
        identity = {
            "operation": operation,
            "kind": kind,
            "recipient": recipient,
            "in_reply_to": meta.get("in_reply_to"),
            "request_id": meta.get("request_id"),
            "broadcast_id": meta.get("broadcast_id"),
            "origin_request_id": meta.get("origin_request_id"),
            "origin_inbound_id": meta.get("origin_inbound_id"),
        }
        origin_key = meta.get("origin_obligation_key_digest")
        roster_revision = meta.get("expected_roster_revision")
        if origin_key is not None:
            identity["origin_obligation_key_digest"] = origin_key
        if roster_revision is not None:
            identity["expected_roster_revision"] = roster_revision
        return identity

    @staticmethod
    def _operation_intent_digest(intent: dict) -> str:
        canonical = json.dumps(
            intent,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _operation_digest_for_intent(cls, intent: dict, body: str) -> str:
        return cls._operation_intent_digest({**intent, "body": body})

    def read_operation_intent(self, sender: str, operation_nonce: str) -> dict | None:
        """Read one atomically prepared wrapper-operation identity."""
        path = self._operation_intent_path(sender, operation_nonce)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("sender") != sender
            or value.get("operation_nonce") != operation_nonce
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("operation_digest", "")))
            is None
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("payload_sha256", "")))
            is None
            or (
                "intent_digest" in value
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(value.get("intent_digest", "")),
                )
                is None
            )
            or not isinstance(value.get("payload_size"), int)
            or value.get("state") not in {"prepared", "published"}
        ):
            return None
        return value

    def _record_operation_intent_locked(
        self,
        *,
        sender: str,
        operation_nonce: str,
        operation_digest: str,
        body: str,
        intent_digest: str | None = None,
    ) -> tuple[Path, dict]:
        if re.fullmatch(r"[0-9a-f]{64}", operation_digest) is None:
            raise ValueError(
                "operation digest must be exactly 64 lowercase hexadecimal characters"
            )
        path = self._operation_intent_path(sender, operation_nonce)
        payload = body.encode("utf-8")
        identity = {
            "sender": sender,
            "operation_nonce": operation_nonce,
            "operation_digest": operation_digest,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_size": len(payload),
        }
        if intent_digest is not None:
            if re.fullmatch(r"[0-9a-f]{64}", intent_digest) is None:
                raise ValueError(
                    "operation intent digest must be exactly 64 lowercase hexadecimal characters"
                )
            identity["intent_digest"] = intent_digest
        existing = self.read_operation_intent(sender, operation_nonce)
        if isinstance(existing, dict):
            base_identity = {
                name: value
                for name, value in identity.items()
                if name != "intent_digest"
            }
            if any(existing.get(name) != value for name, value in base_identity.items()):
                raise ValueError("operation nonce was already used with a different payload")
            if intent_digest is not None:
                existing_intent = existing.get("intent_digest")
                if existing_intent not in {None, intent_digest}:
                    raise ValueError(
                        "operation nonce was already used with a different intent"
                    )
                if existing_intent is None:
                    existing["intent_digest"] = intent_digest
                    _atomic_write_text(
                        path,
                        json.dumps(existing, indent=2, ensure_ascii=False),
                    )
            return path, existing
        if path.exists():
            raise ValueError("operation intent marker is unreadable")
        intent = {**identity, "state": "prepared", "prepared_at": _now_iso()}
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(intent, indent=2, ensure_ascii=False))
        return path, intent

    def record_operation_intent(
        self,
        *,
        sender: str,
        operation_nonce: str,
        operation_digest: str,
        body: str,
        operation_intent: dict | None = None,
    ) -> dict:
        """Durably mark a complete captured payload before canonical append."""
        intent_digest = None
        if operation_intent is not None:
            if not isinstance(operation_intent, dict):
                raise ValueError("operation intent must be a mapping")
            if self._operation_digest_for_intent(operation_intent, body) != operation_digest:
                raise ValueError("operation intent does not match the operation digest")
            intent_digest = self._operation_intent_digest(operation_intent)
        lock = self.state_dir / "operation-publication.lock"
        with self._exclusive_lock(
            lock,
            timeout=10.0,
            what="wrapper operation publication",
        ):
            _path, intent = self._record_operation_intent_locked(
                sender=sender,
                operation_nonce=operation_nonce,
                operation_digest=operation_digest,
                body=body,
                intent_digest=intent_digest,
            )
            return dict(intent)

    def send_operation(
        self,
        *,
        sender: str,
        recipient: str,
        body: str,
        kind: str,
        subject: str = "",
        meta: dict,
        operation_nonce: str,
        operation_digest: str,
    ) -> tuple[Message, bool]:
        """Atomically dedupe one canonical wrapper operation publication."""
        if re.fullmatch(r"[0-9a-f]{32}", operation_nonce) is None:
            raise ValueError("operation nonce must be exactly 32 lowercase hexadecimal characters")
        if re.fullmatch(r"[0-9a-f]{64}", operation_digest) is None:
            raise ValueError("operation digest must be exactly 64 lowercase hexadecimal characters")
        supplied = dict(meta)
        if supplied.get("operation_nonce") not in {None, operation_nonce}:
            raise ValueError("operation nonce metadata conflicts with publication request")
        if supplied.get("operation_digest") not in {None, operation_digest}:
            raise ValueError("operation digest metadata conflicts with publication request")
        supplied["operation_nonce"] = operation_nonce
        supplied["operation_digest"] = operation_digest
        operation_intent = self._operation_intent_identity(
            operation="composing" if kind == "composing" else "terminal",
            kind=kind,
            recipient=recipient,
            meta=supplied,
        )
        if self._operation_digest_for_intent(operation_intent, body) != operation_digest:
            raise ValueError("operation intent does not match the operation digest")
        intent_digest = self._operation_intent_digest(operation_intent)
        lock = self.state_dir / "operation-publication.lock"
        with self._exclusive_lock(
            lock,
            timeout=10.0,
            what="wrapper operation publication",
        ):
            intent_path, intent = self._record_operation_intent_locked(
                sender=sender,
                operation_nonce=operation_nonce,
                operation_digest=operation_digest,
                body=body,
                intent_digest=intent_digest,
            )
            for existing in self.valid_messages():
                existing_meta = existing.meta or {}
                if (
                    existing.sender != sender
                    or existing_meta.get("operation_nonce") != operation_nonce
                ):
                    continue
                if existing_meta.get("operation_digest") == operation_digest:
                    if intent.get("state") != "published" or intent.get(
                        "message_id"
                    ) != existing.id:
                        intent["state"] = "published"
                        intent["message_id"] = existing.id
                        intent["published_at"] = _now_iso()
                        _atomic_write_text(
                            intent_path,
                            json.dumps(intent, indent=2, ensure_ascii=False),
                        )
                    return existing, False
                raise ValueError("operation nonce was already used with a different payload")
            message = self.send(
                sender=sender,
                recipient=recipient,
                body=body,
                kind=kind,
                subject=subject,
                meta=supplied,
            )
            intent["state"] = "published"
            intent["message_id"] = message.id
            intent["published_at"] = _now_iso()
            _atomic_write_text(
                intent_path,
                json.dumps(intent, indent=2, ensure_ascii=False),
            )
            return message, True

    def send_operator_answer_atomic(
        self,
        *,
        actor: str,
        request_id: str,
        body: str,
        subject: str | None = None,
        extra_meta: dict | None = None,
        expected_recipient: str | None = None,
        lock_timeout: float = 10.0,
    ) -> OperatorAnswerSendResult:
        """Atomically re-check and emit a final operator answer.

        This is the shared final-send path for CLI relay, roster-agent
        ``answer_escalation`` intents, and authenticated dashboard lead-chat
        answers. Callers must not already hold ``_config_lock``: the lock is
        intentionally non-reentrant.
        """
        try:
            with self._config_lock(timeout=lock_timeout):
                from agenttalk import threads as _threads

                try:
                    resolved = _threads.resolve_operator_answer_target(
                        self, actor, request_id)
                except Exception as e:
                    return OperatorAnswerSendResult(
                        False,
                        denial_code="operator_answer_state_unreadable",
                        detail=str(e),
                    )
                if not resolved.ok:
                    return OperatorAnswerSendResult(
                        False,
                        denial_code=resolved.denial_code or "operator_answer_denied",
                        detail=resolved.detail,
                    )
                recipient = resolved.recipient or ""
                if expected_recipient is not None and recipient != expected_recipient:
                    return OperatorAnswerSendResult(
                        False,
                        denial_code="operator_answer_recipient_mismatch",
                        detail=(f"resolved recipient {recipient!r} does not match "
                                f"expected recipient {expected_recipient!r}"),
                    )
                meta = dict(extra_meta or {})
                meta["request_id"] = request_id
                meta["operator_answer"] = "true"
                meta["operator_origin"] = actor
                try:
                    msg = self.send(
                        sender=actor,
                        recipient=recipient,
                        kind="message",
                        subject=subject or f"operator answer ({request_id})",
                        body=body,
                        meta=meta,
                        _allow_reserved_sender=actor in _avatars.RESERVED_PRINCIPALS,
                        _config_locked=True,
                    )
                except (OSError, ValueError) as e:
                    return OperatorAnswerSendResult(
                        False,
                        denial_code="operator_answer_send_rejected",
                        detail=str(e),
                        failed=True,
                    )
                return OperatorAnswerSendResult(True, message=msg)
        except TimeoutError as e:
            return OperatorAnswerSendResult(
                False, denial_code="operator_answer_lock_unavailable",
                detail=str(e),
            )
        except Exception as e:
            return OperatorAnswerSendResult(
                False, denial_code="operator_answer_state_unreadable",
                detail=str(e),
            )

    # --------------------------------------------------------------- reading

    def _scan_messages_with_paths(
        self, *, since_id: str | None = None,
    ) -> tuple[list[tuple[Message, Path]], list[tuple[Path, str, str]]]:
        """The canonical disk walk, keeping each verdict paired with ITS file.

        Returns ``(valid, invalid)`` where valid is ``[(Message, path)]``
        and invalid is ``[(path, ident, reason)]``. Pairing the verdict
        with the source path at scan time is what makes quarantine safe:
        an ident is NOT a file identity (an invalid file may embed an id
        that collides with another file's stem — Codex WP01 review
        repro), so any after-the-fact ident→path mapping can misresolve.

        ``since_id`` is the delivery-hot-path fast skip (perf fix #1): a
        file whose stem is ``<= since_id`` is dropped BEFORE it is read or
        parsed, so a poller that already consumed everything up to its
        cursor pays ~no per-file open/parse/validate cost as the store
        grows. This is sound for DELIVERY only — filenames are ``<id>.json``
        and ``stem == id`` is enforced below, so a skipped valid file has
        ``id <= since_id`` and would be filtered anyway, and a skipped
        forged file (stem mismatching a higher embedded id) is one we'd
        never deliver. It is NOT sound for tamper visibility, so the
        invalid report / quarantine callers MUST NOT pass ``since_id``
        (they keep full-scanning). ``None`` = full scan (current behavior).
        """
        valid: list[tuple[Message, Path]] = []
        invalid: list[tuple[Path, str, str]] = []
        if not self.messages_dir.exists():
            return valid, invalid
        for p in sorted(self.messages_dir.iterdir()):
            if p.suffix != ".json":
                continue
            # Fast skip BEFORE any read/parse: stem == id is enforced just
            # below for delivered files, and ids sort lexically, so a stem
            # <= since_id cannot become a deliverable id > since_id.
            if since_id and p.stem <= since_id:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError as e:
                invalid.append((p, p.stem, f"cannot read file: {e}"))
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                invalid.append((p, p.stem, f"invalid JSON: {e}"))
                continue
            try:
                msg = Message.from_raw(data)
            except ValueError as e:
                ident = data.get("id") if isinstance(data, dict) else None
                if not isinstance(ident, str) or not ident:
                    ident = p.stem
                invalid.append((p, ident, str(e)))
                continue
            if p.stem != msg.id:
                # The file name must equal the embedded id — send() is the
                # only writer and always names files <id>.json. A mismatch is
                # a forged/corrupt/renamed file: a low-sorting name carrying a
                # high (e.g. future-dated) embedded id would otherwise be
                # delivered and poison the cursor, stranding real lower-id
                # messages (review H1). Quarantine it instead of delivering.
                invalid.append((p, msg.id,
                                f"filename stem {p.stem!r} does not match "
                                f"embedded id {msg.id!r}"))
                continue
            valid.append((msg, p))
        return valid, invalid

    def _scan_messages(
        self, *, since_id: str | None = None,
    ) -> tuple[list[Message], list[tuple[str, str]]]:
        """Read every file in messages/ once, separating valid messages
        from invalid ones. Returns (valid, invalid) where invalid is
        [(file_stem_or_id, reason)].

        This is the canonical read path — never construct a Message
        from disk JSON without going through here. Catches JSON
        parse errors, shape/type errors, and missing fields *before*
        downstream callers can crash on `data["id"]` or compare a
        numeric id against a string cursor. (Since 0.15.0 this is a
        projection of ``_scan_messages_with_paths`` — one walk, one
        gate set.)

        ``since_id`` forwards the delivery-hot-path fast skip; see
        ``_scan_messages_with_paths``. Delivery callers only.
        """
        valid_p, invalid_p = self._scan_messages_with_paths(since_id=since_id)
        return ([m for m, _ in valid_p],
                [(ident, reason) for _, ident, reason in invalid_p])

    def all_messages(self) -> list[Message]:
        """Return all parseable + schema-valid messages.

        Roster validation is applied in ``messages_for``; this method
        returns everything that constructed cleanly, so transcript
        export still sees messages from old sessions whose agents are
        no longer in the current roster.
        """
        valid, _ = self._scan_messages()
        return valid

    def _invalid_file_entries(self) -> list[tuple[Path, str, str]]:
        """ONE path-aware walk over the FULL gate set (parse + schema +
        roster + signature). Returns ``[(path, ident, reason)]``.

        Both the invalid REPORT (`list_invalid_messages`) and the
        quarantine SELECTION (`list_invalid_message_paths`) are pure
        projections of this list — FR-011 lockstep by construction, and
        every verdict is paired with its own source file at scan time
        (an ident can collide across files; a path cannot).
        """
        try:
            cfg = self.load_config()
        except (ValueError, OSError, FileNotFoundError):
            cfg = {}
        # D3 (#19): history is validated against the KNOWN roster (active ∪
        # retired) so a message from a now-retired identity stays valid — a
        # tombstone must not turn its own past messages into "invalid" debris.
        roster = self._known_roster(cfg)
        require_sig = self.signing_enforced()
        project_id = self.project_id() if require_sig else None
        key: bytes | None = None
        if require_sig:
            try:
                key = _signing.load_key(project_id)
            except (FileNotFoundError, OSError, ValueError):
                key = None
        valid_p, parse_failures = self._scan_messages_with_paths()
        out: list[tuple[Path, str, str]] = list(parse_failures)
        for m, p in valid_p:
            try:
                m.validate(roster)
            except ValueError as e:
                out.append((p, m.id, str(e)))
                continue
            if require_sig:
                if key is None:
                    out.append((p, m.id,
                                "signatures enforced but no key file is loadable"))
                    continue
                try:
                    _signing.verify_message(
                        m.to_dict(), key, expected_key_id=project_id,
                    )
                except ValueError as e:
                    out.append((p, m.id, str(e)))
        return out

    def list_invalid_messages(self) -> list[tuple[str, str]]:
        """Return [(id_or_stem, reason)] for every message file that
        failed parse, schema, roster, OR signature validation.
        Surfaces everything ``messages_for()`` silently skipped so
        tampering is visible rather than invisible. Used by
        ``agenttalk status`` and ``agenttalk doctor``. (Projection of
        ``_invalid_file_entries`` since 0.15.0.)
        """
        return [(ident, reason) for _, ident, reason in self._invalid_file_entries()]

    # ----------------------------------------------------- quarantine (#17)
    #
    # `prune --invalid` MOVES validation-failing message files into
    # `.agenttalk/quarantine/` — recoverable (restore = move the file
    # back into messages/ by hand), never overwritten, never deleted by
    # the tool. The selection is DRIVEN BY `list_invalid_messages` (the
    # exact ids status/doctor report — FR-011 lockstep by construction),
    # resolved to concrete files. The quarantine dir is a sibling of
    # messages/, so message scanning can never see quarantined files.
    # Safety was established in the 0.14.0 cycle: thread derivation is a
    # pure function of valid messages, cursors are id strings with no
    # contiguity requirement, and HMAC is per-message with no chain —
    # moving invalid files cannot affect any valid-message behavior.

    @property
    def quarantine_dir(self) -> Path:
        return self.dir / "quarantine"

    def quarantined_count(self) -> int:
        """Number of files currently held in quarantine (0 if none)."""
        if not self.quarantine_dir.is_dir():
            return 0
        return sum(1 for p in self.quarantine_dir.iterdir() if p.is_file())

    def list_invalid_message_paths(self) -> list[tuple[Path, str, str]]:
        """The invalid selection WITH file identity: ``[(path, ident, reason)]``.

        A pure projection of the same single gate walk that powers
        ``list_invalid_messages`` — each verdict was paired with its own
        source file at scan time, so an embedded id colliding with
        another file's stem can never misresolve (Codex WP01 review
        repro: valid ``aaa.json`` + invalid ``zzz.json`` embedding id
        ``aaa`` must select ``zzz.json``).
        """
        return self._invalid_file_entries()

    def quarantine_invalid(self, *, dry_run: bool = False) -> list[dict]:
        """Move (or, with ``dry_run``, plan to move) invalid files to quarantine.

        Returns one record per selected file:
        ``{"id", "reason", "from", "to"}``. Collisions in the quarantine
        dir get a timestamp suffix (the ``_archive_session`` precedent):
        the tool NEVER overwrites and NEVER deletes. Valid files are
        untouched by construction — the selection is the path-paired
        gate walk itself, never an ident lookup.
        """
        records: list[dict] = []
        for src, ident, reason in self.list_invalid_message_paths():
            dst = self.quarantine_dir / src.name
            if dst.exists():
                dst = self.quarantine_dir / (
                    f"{src.name}.{_now_iso().replace(':', '-')}"
                )
            records.append({"id": ident, "reason": reason,
                            "from": str(src), "to": str(dst)})
            if not dry_run:
                self.quarantine_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        return records

    # ------------------------------------------------- dead-letter (poison)
    #
    # A POISON message is a VALID, well-signed message the wrapped model cannot
    # process: the turn fails DETERMINISTICALLY, so the at-least-once loop
    # re-delivers it forever (the 0.30.0 supervisor restart-loop limitation).
    # Dead-lettering MOVES the original bytes into a SCAN-INVISIBLE sink
    # (.agenttalk/dead-letter/<agent>/, a sibling of messages/ that no scan
    # walks) and advances the cursor PAST it, so the inbox proceeds. Distinct
    # from quarantine (an invalid/forged FILE = a TRUST failure); this is a
    # valid file = a DELIVERY failure -> separate dir + verbs. Recoverable via
    # `dead-letter list/show/requeue`; reset PRESERVES the sink (like quarantine).
    #
    # The per-agent ATTEMPT LEDGER (state/dead-letter-attempts/<agent>.json) is
    # the DURABLE counter that survives a supervisor RELAUNCH (only `reset`, which
    # clears state/, resets it). It mirrors the per-agent-state convention exactly:
    # SINGLE-WRITER (the wrapper is the sole consumer of its inbox, recv_api.py),
    # UNLOCKED, atomic-write, degrade-to-empty read that NEVER errs high (a torn
    # ledger reading "lots of attempts" would FALSE-dead-letter a healthy message).

    @property
    def dead_letter_dir(self) -> Path:
        return self.dir / "dead-letter"

    def _attempts_path(self, agent: str) -> Path:
        return self.state_dir / "dead-letter-attempts" / f"{validate_agent_name(agent)}.json"

    def dead_letter_attempts(self, agent: str) -> dict:
        """The durable attempt ledger for ``agent`` -> ``{schema_version, agent,
        messages: {msg_id: record}}``. NEVER raises and NEVER errs HIGH: a
        missing/torn/corrupt/non-dict file reads as no attempts (mirror
        read_threadstate), so a healthy message is never FALSE-dead-lettered."""
        empty = {"schema_version": 1, "agent": agent, "messages": {}}
        p = self._attempts_path(agent)
        if not p.exists():
            return empty
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return empty
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
            return empty
        return data

    def _write_attempts(self, agent: str, data: dict) -> None:
        p = self._attempts_path(agent)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(p, json.dumps(data, indent=2, ensure_ascii=False))

    def attempt_record(self, agent: str, msg_id: str) -> dict | None:
        rec = self.dead_letter_attempts(agent)["messages"].get(msg_id)
        return rec if isinstance(rec, dict) else None

    def record_attempt_start(self, agent: str, record: dict, *, attempt_id: str,
                             at: str) -> dict:
        """WRITE-AHEAD: increment ``attempts_started`` + mark ``in_progress`` BEFORE
        ``drive()``. A hard crash mid-turn still leaves a durable started attempt the
        next run reads (-> crash_mid_turn). EXACTLY one call per drive() = one attempt."""
        data = self.dead_letter_attempts(agent)
        mid = record.get("id")
        rec = data["messages"].get(mid)
        if not isinstance(rec, dict):
            rec = {
                "id": mid, "cursor_before": self.cursor(agent),
                "request_id": record.get("request_id"),
                "broadcast_id": record.get("broadcast_id"),
                "from": record.get("from"), "to": record.get("to"),
                "kind": record.get("kind"), "subject": record.get("subject"),
                "first_started_at": at, "attempts_started": 0,
                "poison_eligible_failures": 0, "infra_failures": 0,
                "ambiguous_failures": 0, "last_failure_class": None,
                "last_failure_summary": None, "escalated": False, "in_progress": False,
            }
        rec["attempts_started"] = _safe_int(rec.get("attempts_started")) + 1
        rec["last_started_at"] = at
        rec["last_attempt_id"] = attempt_id
        rec["in_progress"] = True
        data["messages"][mid] = rec
        self._write_attempts(agent, data)
        return rec

    def record_attempt_result(self, agent: str, msg_id: str, *, failure_class: str,
                              summary: str | None, at: str) -> dict | None:
        """After a FAILED drive: clear ``in_progress``, bump the per-class failure
        counter, record the last class/summary. (Success calls clear_attempt.)"""
        data = self.dead_letter_attempts(agent)
        rec = data["messages"].get(msg_id)
        if not isinstance(rec, dict):
            return None
        rec["in_progress"] = False
        rec["last_failure_class"] = failure_class
        rec["last_failure_summary"] = (summary or "")[:500]
        rec["last_failure_at"] = at
        key = {"poison_eligible": "poison_eligible_failures",
               "known_global_infra": "infra_failures"}.get(
                   failure_class, "ambiguous_failures")
        rec[key] = _safe_int(rec.get(key)) + 1
        if failure_class != "poison_eligible":
            # poison_eligible_failures is CONSECUTIVE (V1 rubric: per-id, reset on progress) -
            # a non-poison outcome (infra / ambiguous) BREAKS the poison run, so only K
            # CONSECUTIVE deterministic-poison classifications auto-DL@K_poison. Closes the
            # interleaved-outage accumulation that could otherwise DL a healthy message at the
            # low cap (lead 5th-verify P2) + defense-in-depth vs residual marker misclassification.
            rec["poison_eligible_failures"] = 0
        data["messages"][msg_id] = rec
        self._write_attempts(agent, data)
        return rec

    def reconcile_crash_in_progress(self, agent: str, msg_id: str, *, at: str) -> bool:
        """On relaunch: a stale ``in_progress`` for ``msg_id`` means the process crashed
        mid-turn. The CAUSE is UNOBSERVED (could be a healthy-but-slow message the
        supervisor stale-killed, OOM, power-loss, or genuine message-poison), so codex
        ruled crash_mid_turn = AMBIGUOUS, not low-cap poison: it disposes only at the high
        K_escalate ceiling (escalate + last-resort DL), never false-DL@3 a healthy-but-slow
        message. The already-incremented attempts_started (write-ahead) counts it toward
        that ceiling; here we just record the class + clear in_progress. Returns True if
        reconciled."""
        data = self.dead_letter_attempts(agent)
        rec = data["messages"].get(msg_id)
        if not isinstance(rec, dict) or not rec.get("in_progress"):
            return False
        rec["in_progress"] = False
        rec["ambiguous_failures"] = _safe_int(rec.get("ambiguous_failures")) + 1
        rec["poison_eligible_failures"] = 0   # a crash (ambiguous) breaks the consecutive poison run
        rec["last_failure_class"] = "ambiguous_or_unknown"
        rec["last_failure_summary"] = "crash_mid_turn"
        rec["last_failure_at"] = at
        data["messages"][msg_id] = rec
        self._write_attempts(agent, data)
        return True

    def mark_attempt_escalated(self, agent: str, msg_id: str, *, routed: bool = False) -> None:
        """Latch that the high-attempt backstop escalation fired for ``msg_id`` and record
        whether the operator notice actually ROUTED. An escalated-but-unrouted record is
        the signal doctor surfaces LOUD (no escalation target resolved), so a known-infra
        outage that loops under backoff is never silent."""
        data = self.dead_letter_attempts(agent)
        rec = data["messages"].get(msg_id)
        if isinstance(rec, dict):
            rec["escalated"] = True
            rec["escalation_routed"] = bool(routed)
            data["messages"][msg_id] = rec
            self._write_attempts(agent, data)

    def list_unrouted_escalations(self) -> list[dict]:
        """Every attempt record that hit the escalation backstop but whose operator notice
        did NOT route (no liaison/lead resolved). Doctor reports these LOUD - a known-infra
        message can otherwise loop under backoff forever with no operator-visible signal.
        Reads all per-agent ledgers; degrade-safe (skips unreadable)."""
        d = self.state_dir / "dead-letter-attempts"
        if not d.is_dir():
            return []
        out: list[dict] = []
        for p in sorted(d.glob("*.json")):
            agent = p.stem
            for mid, rec in (self.dead_letter_attempts(agent).get("messages") or {}).items():
                if isinstance(rec, dict) and rec.get("escalated") and not rec.get("escalation_routed"):
                    out.append({"agent": agent, "message_id": mid,
                                "attempts": rec.get("attempts_started"),
                                "last_failure_class": rec.get("last_failure_class")})
        return out

    def clear_attempt(self, agent: str, msg_id: str) -> None:
        data = self.dead_letter_attempts(agent)
        if data["messages"].pop(msg_id, None) is not None:
            self._write_attempts(agent, data)

    def gc_attempts_below(self, agent: str, cursor: str) -> None:
        """Drop attempt records at/below the committed cursor (bounded ledger)."""
        if not cursor:
            return
        data = self.dead_letter_attempts(agent)
        drop = [mid for mid in data["messages"] if mid <= cursor]
        if drop:
            for mid in drop:
                data["messages"].pop(mid, None)
            self._write_attempts(agent, data)

    def dead_letter(self, agent: str, record: dict, *, reason: str | None,
                    failure_class: str, at: str,
                    child_output_tail: object | None = None) -> Path:
        """Dispose the poison HEAD ``record``: move its bytes to the scan-invisible
        sink + advance the cursor past it, as ONE ordered, fail-closed sequence
        (single-writer = serialized): identity-check -> size/SHA256 -> MOVE
        (collision-safe, never overwrite/delete) -> sidecar -> clear attempt ->
        advance_cursor(the LIVE head id, never a ledger/sidecar id) LAST + GC.

        INVARIANT: never advance the cursor unless the original bytes are recoverable
        in the sink - the MOVE precedes the advance, and a write failure fails CLOSED
        (no advance). A crash mid-dispose is therefore LOSSLESS: the bytes are already
        in the sink, surfaced by :meth:`list_dead_letters` / :meth:`dead_lettered_count`
        (as an orphan payload if the crash preceded the sidecar write).

        RECOVERY (honest note): through the production loop this method is NOT re-invoked
        for the same id after a crash - :func:`recv_api.next_record` scans only
        ``messages/`` and the file has already moved to the sink, so next_record skips
        the (now-missing) id and the cursor advances naturally past it once the NEXT
        message commits. The direct-call idempotent no-op-move replay (re-calling
        dead_letter for the same id: ``payload.exists() and not src.exists()`` -> just
        clear+advance) is exercised by tests (test_12) but is NOT the production recovery
        path. If no further traffic arrives, a lingering attempt-ledger entry / behind
        cursor is benign (bytes are safe + surfaced); an idle/startup reconcile +
        doctor-warn-on-stuck-in_progress is a tracked fast-follow."""
        mid = record.get("id")
        if not (isinstance(mid, str) and _ID_RE.match(mid)):
            raise ValueError(f"dead_letter: record id {mid!r} is not a valid message id")
        sink = self.dead_letter_dir / validate_agent_name(agent)
        payload = sink / f"{mid}.json"
        sidecar = sink / f"{mid}.deadletter.json"
        src = self.messages_dir / f"{mid}.json"
        sink.mkdir(parents=True, exist_ok=True)
        attempt = self.attempt_record(agent, mid) or {}
        if src.exists():
            # SOURCE-IDENTITY: the live file stem == the record id by construction
            # (src is messages/<mid>.json). Move bytes FIRST so they are recoverable
            # before the cursor can ever advance.
            body = src.read_bytes()
            dst = payload
            sidecar_dst = sidecar
            if dst.exists():   # never overwrite a prior payload OR its sidecar (collision-safe)
                # Name the collision sibling <mid>.<iso>.json (+ .<iso>.deadletter.json) so the
                # endswith('.json') readers (count / list / read_payload) still SURFACE it
                # (lead C1); the file STEM <mid>.<iso> becomes its unique recovery id. The old
                # <mid>.json.<iso> scheme was invisible to those filters = unrecoverable.
                suffix = _now_iso().replace(":", "-")
                dst = sink / f"{mid}.{suffix}.json"
                sidecar_dst = sink / f"{mid}.{suffix}.deadletter.json"
            shutil.move(str(src), str(dst))
            meta = {
                "schema_version": 1, "message_id": mid, "agent": agent,
                "from": record.get("from"), "to": record.get("to"),
                "subject": record.get("subject"), "kind": record.get("kind"),
                "request_id": record.get("request_id"),
                "broadcast_id": record.get("broadcast_id"),
                "attempts": _safe_int(attempt.get("attempts_started")),
                "class": failure_class, "last_reason": reason,
                "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "first_at": attempt.get("first_started_at"), "last_at": at,
                "deadlettered_at": at, "cursor_at_deadletter": self.cursor(agent),
                "payload_path": str(dst),
            }
            tail = normalize_child_output_tail(child_output_tail)
            if tail is not None:
                meta["child_output_tail"] = tail
            _atomic_write_text(sidecar_dst, json.dumps(meta, indent=2, ensure_ascii=False))
        elif not payload.exists():
            # bytes neither in messages/ nor in the sink -> NOT recoverable -> NEVER
            # advance the cursor (fail closed).
            raise FileNotFoundError(
                f"dead_letter: message {mid} is neither in messages/ nor the sink")
        # else: replay of a crashed disposition (payload already in sink) -> no-op
        # move; fall through to complete the clear + advance.
        self.clear_attempt(agent, mid)
        self.advance_cursor(agent, mid)   # LAST; only reached once bytes are recoverable
        self.gc_attempts_below(agent, mid)
        return payload

    def dead_lettered_count(self, agent: str | None = None) -> int:
        """Count dead-lettered message payloads (excludes .deadletter.json sidecars)."""
        root = self.dead_letter_dir
        if not root.is_dir():
            return 0
        agent_dirs = ([root / validate_agent_name(agent)] if agent
                      else [d for d in root.iterdir() if d.is_dir()])
        n = 0
        for d in agent_dirs:
            if d.is_dir():
                n += sum(1 for p in d.iterdir()
                         if p.is_file() and _is_dead_letter_payload(p.name))
        return n

    def list_dead_letters(self, agent: str | None = None) -> list[dict]:
        """Return one dict per dead-lettered message, keyed off the PAYLOAD files so the
        list AGREES with :meth:`dead_lettered_count` (both count payloads) and an ORPHAN
        payload whose sidecar write was interrupted is still surfaced (metadata recoverable
        from the payload name), not silently dropped (lead F1). Sidecar metadata is
        attached when present; degrade-safe. Sorted by message_id (chronological)."""
        root = self.dead_letter_dir
        if not root.is_dir():
            return []
        agent_dirs = ([root / validate_agent_name(agent)] if agent
                      else sorted(d for d in root.iterdir() if d.is_dir()))
        out: list[dict] = []
        for d in agent_dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if not (p.is_file() and _is_dead_letter_payload(p.name)):
                    continue
                # The file STEM is the canonical recovery id (read_dead_letter_payload reads
                # <stem>.json), so report it AS message_id even when the sidecar records the
                # original id - else a collision sibling (<mid>.<iso>.json) would list/requeue
                # under the original <mid> and resolve to the FIRST payload (lead C1).
                stem = p.name[:-len(".json")]
                sidecar = d / f"{stem}.deadletter.json"
                meta: dict = {}
                meta_loaded = False
                if sidecar.is_file():
                    try:
                        loaded = json.loads(sidecar.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            meta = loaded
                            meta_loaded = True
                    except (OSError, ValueError):
                        meta = {}
                meta.setdefault("agent", d.name)
                orig = meta.get("message_id")
                meta["message_id"] = stem
                if orig and orig != stem:
                    meta["original_message_id"] = orig   # collision sibling: audit the source id
                if not meta_loaded:
                    # bytes recoverable, but the metadata is lost - flag whether the sidecar was
                    # MISSING or merely UNREADABLE (corrupt JSON / wrong shape) so the operator
                    # is not misled into thinking the metadata simply was not there (verify C1).
                    meta["orphan_no_sidecar"] = True
                    if sidecar.is_file():
                        meta["sidecar_unreadable"] = True
                out.append(meta)
        out.sort(key=lambda m: str(m.get("message_id") or ""))
        return out

    def read_dead_letter_payload(self, agent: str, msg_id: str) -> bytes | None:
        """The original message bytes for a dead-lettered id, or None.

        SECURITY (reviewer-2 F5): msg_id is caller-supplied. PATH-BIND the resolved payload
        inside the agent sink so a traversal id (e.g. ``..\\..\\config``) can never escape to
        read an arbitrary .agenttalk file; an escaping id degrades to None (not found)."""
        sink = self.dead_letter_dir / validate_agent_name(agent)
        p = sink / f"{msg_id}.json"
        try:
            p.resolve().relative_to(sink.resolve())
        except ValueError:
            return None
        if not p.is_file():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def valid_messages(self) -> list[Message]:
        """Return every roster- AND signature-valid message, ALL recipients.

        Same trust gate as ``messages_for`` (schema + roster + — when
        ``signing_enforced()`` — HMAC signature) but WITHOUT the
        single-recipient filter. This is the input thread derivation
        (``agenttalk threads`` / status warnings) must use: deriving
        from ``all_messages()`` instead would let a forged or unsigned
        ``review-result`` / ``proposal-response`` falsely close a real
        open thread even though ``wait`` / ``recv`` would have skipped
        it. Sorted by id (chronological).
        """
        return self._validated_messages()

    def publication_ordered_messages(
        self,
        messages: list[Message] | None = None,
    ) -> list[Message]:
        """Return validated messages in the best available publication order.

        Use ``publication_ordered_messages_with_durability`` when correctness
        depends on whether every returned position is backed by the sidecar.
        """
        ordered, _ = self.publication_ordered_messages_with_durability(messages)
        return ordered

    def publication_ordered_messages_with_durability(
        self,
        messages: list[Message] | None = None,
    ) -> tuple[list[Message], bool]:
        """Return ``(messages, durable_order)`` for one sidecar read.

        Legacy stores have no sidecar, so their established id order is the only
        available bootstrap authority. The first subsequent send freezes that
        order under the publication lock before reserving its own sequence. A
        partial sidecar similarly folds orphan messages onto its tail in id order.
        ``durable_order`` is false for both reconstructed shapes, remains false
        after that reconstruction is persisted, and is true only when the
        sidecar covers every returned message and explicitly records that its
        history was not reconstructed. Older unmarked sidecars fail closed.
        """
        messages = self.valid_messages() if messages is None else list(messages)
        order = self._read_message_publication_order()
        if order is None:
            return messages, False
        sequences = order["messages"]
        missing = [message.id for message in messages if message.id not in sequences]
        durable_order = not missing and order.get("order_reconstructed") is False
        if missing:
            # Read-time heal, IN-MEMORY only (a read path must not write): fold the
            # orphans onto the tail in id-order — identical to what the next write
            # will persist, so a read before and after the write agree — instead of
            # wedging every ordered read under version skew (#37).
            sequences = self._extend_order_with_orphans(order, missing)["messages"]
        return (
            sorted(messages, key=lambda message: sequences[message.id]),
            durable_order,
        )

    @staticmethod
    def _stable_scan_problem_reason(reason: str) -> str:
        """Map platform/version-specific scan details to a stable reason code."""
        if reason.startswith("cannot read file:"):
            return "message_file_unreadable"
        if reason.startswith("invalid JSON:"):
            return "message_json_invalid"
        if reason.startswith("filename stem "):
            return "message_filename_mismatch"
        return "message_schema_invalid"

    def _validated_message_snapshot(
        self,
        *,
        since_id: str | None = None,
        collect_problems: bool,
    ) -> tuple[
        list[Message],
        list[dict[str, str | None]],
        set[str],
    ]:
        """Run the shared full validation gate over one canonical disk walk.

        The third result is the set of canonical ``.json`` stems observed by
        that same walk.  The public completeness snapshot uses it to distinguish
        a present-but-rejected message from a publication-ordered message whose
        file is absent.

        ``collect_problems=False`` is the delivery hot path.  In particular, it
        preserves the historical fail-closed early return when no usable roster
        exists, without touching the message directory.
        """
        try:
            cfg = self.load_config()
            # D3 (#19): validate history against the KNOWN roster (active ∪
            # retired) so a retired identity's past messages stay valid.
            roster = self._known_roster(cfg)
        except (ValueError, OSError, FileNotFoundError):
            roster = []
        if not roster and not collect_problems:
            # Preserve _validated_messages' existing no-roster fast fail-closed
            # behavior.  The diagnostic API still scans so it can explain why
            # every otherwise-parseable file was rejected.
            return [], [], set()

        require_sig = bool(roster) and self.signing_enforced()
        project_id = self.project_id() if require_sig else None
        key: bytes | None = None
        if require_sig:
            try:
                key = _signing.load_key(project_id)
            except (FileNotFoundError, OSError, ValueError):
                key = None  # key vanished between check and load — refuse

        scanned, scan_failures = self._scan_messages_with_paths(
            since_id=since_id,
        )
        present_stems = {
            path.stem for _, path in scanned
        } | {
            path.stem for path, _, _ in scan_failures
        }
        problems: list[dict[str, str | None]] = []

        def reject(ident: str | None, path: Path, reason: str) -> None:
            if collect_problems:
                problems.append(
                    {"id": ident, "path": str(path), "reason": reason}
                )

        for path, ident, reason in scan_failures:
            reject(ident, path, self._stable_scan_problem_reason(reason))

        out: list[Message] = []
        for message, path in scanned:
            if not roster:
                reject(message.id, path, "message_roster_unavailable")
                continue
            try:
                message.validate(roster)
            except ValueError:
                reject(message.id, path, "message_roster_invalid")
                continue
            if require_sig:
                if key is None:
                    reject(message.id, path, "message_signature_key_unavailable")
                    continue
                try:
                    _signing.verify_message(
                        message.to_dict(), key, expected_key_id=project_id,
                    )
                except ValueError:
                    reject(message.id, path, "message_signature_invalid")
                    continue
            out.append(message)

        # Restore the documented "sorted by id (chronological)" contract:
        # _scan_messages_with_paths yields raw filesystem-iteration (filename)
        # order, which equals id order ONLY because stem==id is now enforced
        # above. Sort explicitly so delivery, cursor advance, and thread
        # replay are correct even if that invariant is ever relaxed (review H1).
        out.sort(key=lambda message: message.id)
        # Defensive dedupe by id: stem==id + unique filenames make duplicate
        # ids structurally impossible today, but double-delivery would be a
        # silent correctness bug if that ever changed, so guard it cheaply.
        deduped: list[Message] = []
        seen_ids: set[str] = set()
        for message in out:
            if message.id in seen_ids:
                continue
            seen_ids.add(message.id)
            deduped.append(message)
        return deduped, problems, present_stems

    def validated_messages_with_problems(
        self,
        *,
        since_id: str | None = None,
    ) -> tuple[list[Message], list[dict[str, str | None]]]:
        """Return one full-validation snapshot as ``(valid, problems)``.

        One canonical directory traversal applies schema, known-roster, and
        (when enforced) signature validation.  ``problems`` also includes a
        ``publication_ordered_message_absent`` entry for every durable order id
        whose canonical message file is absent.  That reverse-history gap is
        reported but never auto-healed.

        Problem dictionaries have exactly ``id``, ``path``, and ``reason``.
        Reasons are stable machine-readable codes; paths use the host platform's
        native representation.  ``since_id`` is an exclusive incremental slice,
        so completeness/integrity consumers should leave it as ``None``.
        """
        # send() durably reserves an order entry immediately before publishing
        # its file under this lock.  Covering BOTH the traversal and order read
        # prevents that legitimate interval from looking like deleted history.
        # Retirement uses the same outer lock ordering, so roster removal cannot
        # race the gate and manufacture a mixed-roster snapshot.
        with self._retirement_lock(), self._message_publication_lock():
            valid, problems, present_stems = self._validated_message_snapshot(
                since_id=since_id,
                collect_problems=True,
            )
            order = self._read_message_publication_order()
            if order is None:
                return valid, problems
            ordered_ids = sorted(
                order["messages"],
                key=order["messages"].__getitem__,
            )
            for message_id in ordered_ids:
                if since_id is not None and message_id <= since_id:
                    continue
                if message_id in present_stems:
                    continue
                problems.append(
                    {
                        "id": message_id,
                        "path": str(self.messages_dir / f"{message_id}.json"),
                        "reason": "publication_ordered_message_absent",
                    }
                )
            return valid, problems

    def _validated_messages(self, *, since_id: str | None = None) -> list[Message]:
        """Shared trust gate behind ``messages_for`` and ``valid_messages``.

        Applies schema/roster validation and (when enforced) HMAC
        signature verification to every scanned message, returning the
        survivors in id order. No recipient filtering — callers layer
        that on top.

        ``since_id`` forwards the delivery fast skip into the scan so
        files at or below the cursor are never opened (perf fix #1).
        ``valid_messages`` MUST keep the default ``None`` (full log) —
        epoch / thread / rescind derivation reads the whole history.
        """
        valid, _, _ = self._validated_message_snapshot(
            since_id=since_id,
            collect_problems=False,
        )
        return valid

    def current_epoch(self) -> str | None:
        """The global epoch id = the message id of the latest validated global
        barrier event, or ``None`` if no barrier has fired (#19 Phase A, RFC
        §"Global Epochs And Send-Time Barriers").

        A barrier is an ordinary message carrying
        ``meta.barrier={"version","scope":"global","type"}`` — NO new kind, so
        old clients see a normal note. Visibility is by store-scan, not inbox
        delivery: a single self-addressed barrier is globally authoritative
        because this scans the WHOLE validated log (every recipient). "Latest"
        is by deterministic message-id order (not real time). A malformed
        ``meta.barrier`` is ignored (never counts, never crashes).

        This FAILS OPEN against suppression: a writer who deletes/withholds a
        barrier makes this read the latest *surviving* one. HMAC proves bytes,
        not presence — Phase A is trusted-team correctness, not a malicious-peer
        control (see SECURITY.md).
        """
        latest: str | None = None
        for m in self.valid_messages():
            b = (m.meta or {}).get("barrier")
            if (isinstance(b, dict) and b.get("scope") == "global"
                    and "version" in b and "type" in b):
                if latest is None or m.id > latest:
                    latest = m.id
        return latest

    def messages_for(self, agent: str, *, since_id: str | None = None) -> list[Message]:
        """Return validated messages addressed to ``agent``.

        Silently skips messages that fail schema/roster validation —
        and, when ``signing_enforced()`` is true (i.e. a per-user HMAC
        key file exists for this project), silently skips messages
        missing a valid HMAC signature. Callers (wait, recv) never act
        on unverified input. Use ``list_invalid_messages()`` to see
        what was skipped.
        """
        msgs: list[Message] = []
        # Forward since_id into the scan so files at/below the cursor are
        # never opened (perf fix #1). The post-scan ``m.id <= since_id``
        # check below is kept belt-and-suspenders: it is the semantic
        # source of truth for EXCLUSIVE delivery and stays correct even if
        # the filename==id fast-skip invariant is ever relaxed.
        for m in self._validated_messages(since_id=since_id):
            if m.recipient != agent:
                continue
            if since_id and m.id <= since_id:
                continue
            msgs.append(m)
        return msgs

    def unread_for(self, agent: str) -> list[Message]:
        return self.messages_for(agent, since_id=self.cursor(agent))

    def last_received_for(
        self,
        agent: str,
        *,
        exclude_kinds: frozenset[str] = CONTROL_KINDS,
    ) -> Message | None:
        """Return the most recent valid non-control message addressed to ``agent``,
        or ``None`` if the inbox is empty. Used by ``agenttalk reply``
        to auto-derive the peer + correlate ``request_id``. Control
        kinds (``composing``) are excluded by default so a flurry of
        "still drafting" pings doesn't cause `reply` to correlate to
        a placeholder instead of the real prior message."""
        msgs = self.messages_for(agent)
        for m in reversed(msgs):
            if m.kind in exclude_kinds:
                continue
            return m
        return None

    def cursor(self, agent: str) -> str:
        p = self.state_dir / f"{agent}.cursor"
        if not p.exists():
            return ""
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        # Torn-read guard (0.28.1 / Codex sandbox): under the sandbox the cursor
        # is direct-written (non-atomic, see _atomic.write_text), so a concurrent
        # reader could catch a half-written id. A non-empty value that is NOT a
        # valid message id is treated as NO cursor - a partial id is a strict
        # PREFIX of the real id (lexicographically LOWER), so this biases toward
        # re-seeing a message (DUPLICATE delivery), never SKIPPING one.
        if raw and not _ID_RE.match(raw):
            return ""
        return raw

    def set_cursor(self, agent: str, msg_id: str) -> None:
        p = self.state_dir / f"{agent}.cursor"
        _atomic_write_text(p, msg_id)

    def advance_cursor(self, agent: str, msg_id: str) -> None:
        """Set cursor to msg_id unless it would move backwards."""
        cur = self.cursor(agent)
        if msg_id > cur:
            self.set_cursor(agent, msg_id)

    # ----------------------------------------------------------- heartbeats

    def write_heartbeat(self, agent: str) -> None:
        """Stamp .agenttalk/state/<agent>.heartbeat with the current ISO timestamp.

        Called periodically by `agenttalk wait` so peers can see whether
        someone is actively listening. Pure observability — never required
        for correctness. (Uses the shared write_text, which falls back to a
        direct write inside a Codex sandbox that blocks the temp+rename; see
        _atomic.write_text.)
        """
        p = self.state_dir / f"{agent}.heartbeat"
        _atomic_write_text(p, _now_iso())

    def read_heartbeat(self, agent: str) -> datetime | None:
        """Return the parsed heartbeat timestamp, or None if absent/unreadable."""
        p = self.state_dir / f"{agent}.heartbeat"
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        # Accept either trailing Z or +00:00 form
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        # Heartbeat is observability only — reject any timezone-less file
        # so a malformed write can't crash `status` via naive vs aware
        # datetime subtraction.
        if dt.tzinfo is None:
            return None
        return dt

    @staticmethod
    def _bounded_heartbeat_age_seconds(
        heartbeat: datetime | None,
        now_epoch: float,
    ) -> float | None:
        """Return a nonnegative age only within the allowed future clock skew.

        A timestamp farther in the future than the health protocol's shared
        skew bound is not evidence of liveness. A timestamp inside the bound
        is clamped to age zero so small clock differences remain tolerated.
        """
        if heartbeat is None:
            return None
        try:
            age = float(now_epoch) - heartbeat.timestamp()
        except (OSError, OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(age):
            return None
        if age < -_health.DEFAULT_HEARTBEAT_SKEW_SECONDS:
            return None
        return max(0.0, age)

    # ------------------------------------------------------ waiting markers
    #
    # The `.waiting` file is written by `agenttalk wait` while it is
    # actively blocking, and removed when it stops (message received,
    # timeout, or interrupt). Like the heartbeat, it is STRICTLY
    # observational: `status` reads it to detect "both agents are
    # blocked on each other" soft-deadlocks. Nothing about message
    # delivery, cursor movement, or replies depends on it. A stale file
    # left behind by a crashed shell is expected and handled at read
    # time (status cross-checks heartbeat age + the recorded deadline).

    def _waiting_lock(self, agent: str):
        name = validate_agent_name(agent)
        return self._exclusive_lock(
            self.state_dir / f"{name}.waiting.lock",
            what=f"waiting marker lock for {name}",
        )

    def _unlink_waiting_locked(self, agent: str) -> bool:
        try:
            (self.state_dir / f"{agent}.waiting").unlink()
        except FileNotFoundError:
            return False
        return True

    def _waiting_superseded_path(self, agent: str, wait_token: str) -> Path | None:
        try:
            validate_agent_name(agent)
        except ValueError:
            return None
        if not isinstance(wait_token, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", wait_token):
            return None
        return self.state_dir / "waiting-superseded" / f"{agent}.{wait_token}.json"

    def _mark_waiting_superseded(self, agent: str, previous: dict, replacement: dict) -> None:
        old_token = previous.get("wait_token")
        new_token = replacement.get("wait_token")
        if not isinstance(old_token, str) or not isinstance(new_token, str):
            return
        if old_token == new_token:
            return
        previous_request = previous.get("to_request")
        replacement_request = replacement.get("to_request")
        if not isinstance(previous_request, str) or not previous_request:
            return
        if previous_request != replacement_request:
            return
        if previous.get("kind") != replacement.get("kind"):
            return
        path = self._waiting_superseded_path(agent, old_token)
        if path is None:
            return
        event = {
            "agent": agent,
            "wait_token": old_token,
            "pid": previous.get("pid"),
            "superseded_by_token": new_token,
            "superseded_by_pid": replacement.get("pid"),
            "superseded_at": _now_iso(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, json.dumps(event, ensure_ascii=False))
        except OSError:
            pass

    def write_waiting(self, agent: str, info: dict) -> None:
        """Stamp .agenttalk/state/<agent>.waiting with a JSON liveness record.

        Overwrites any existing marker (a fresh `wait` supersedes a
        stale one). Best-effort: callers treat any write failure as
        non-fatal since this is observability-only. (Shared write_text, with the
        in-sandbox direct-write fallback - see _atomic.write_text.)
        """
        with self._waiting_lock(agent):
            p = self.state_dir / f"{agent}.waiting"
            previous = self.read_waiting(agent)
            if isinstance(previous, dict):
                self._mark_waiting_superseded(agent, previous, info)
            _atomic_write_text(p, json.dumps(info, ensure_ascii=False))

    def read_waiting(self, agent: str) -> dict | None:
        """Return the parsed waiting record, or None if absent/corrupt.

        Never raises — a malformed or partially written marker reads as
        None so `status` degrades to "not waiting" rather than crashing.
        """
        p = self.state_dir / f"{agent}.waiting"
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def wrapper_wait_generation(self, agent: str) -> str | None:
        """Return the current ordinary or managed wrapper generation.

        Ordinary wrapper loops own a ``mode=wrapper-loop`` marker. Managed
        lead loops mirror their lease as ``lead_loop`` + ``managed`` instead.
        Both are observational wrapper markers and carry the same opaque
        generation; all other waiting records are deliberately excluded.
        """
        marker = self.read_waiting(agent)
        if not isinstance(marker, dict):
            return None
        ordinary = marker.get("mode") == "wrapper-loop"
        managed = marker.get("lead_loop") is True and marker.get("managed") is True
        generation = marker.get("wrapper_generation")
        if not (ordinary or managed) or not isinstance(generation, str):
            return None
        if _AWAIT_PATH_TOKEN_RE.fullmatch(generation) is None:
            return None
        return generation

    def foreign_wait_pid(self, agent: str, self_pid: int, *,
                         now: float | None = None,
                         stale_after: float | None = None) -> int | None:
        """Return the PID of ANOTHER live process currently waiting as
        ``agent`` in this store, or None (0.18.0, FR-007).

        Reads the existing ``.waiting`` marker (which records ``pid`` and a
        ``deadline_epoch``). Returns the marker's pid only when it is a
        different process (``pid != self_pid``), the marker is still fresh,
        and that pid is actually alive. A stale or dead owner yields None
        (silent crash recovery), so a starting ``wait`` only warns about a
        genuine concurrent same-agent window.

        Freshness policy (``now`` / ``stale_after``) is passed IN so this
        stays self-contained — the store never imports the CLI's staleness
        constants. Best-effort and fail-quiet: any error reads as None.
        """
        if now is None:
            now = time.time()
        if stale_after is None:
            stale_after = _WAIT_STALE_AFTER_DEFAULT
        try:
            marker = self.read_waiting(agent)
            if not marker:
                return None
            pid = marker.get("pid")
            if not isinstance(pid, int) or pid == self_pid:
                return None
            # Fresh? A bounded wait records a deadline_epoch; treat the
            # marker as stale once it is past the deadline by more than the
            # threshold. An unbounded wait (deadline None) is fresh as long
            # as its owner is alive (the liveness check below decides).
            deadline = marker.get("deadline_epoch")
            if isinstance(deadline, (int, float)) and now > deadline + stale_after:
                return None
            if not _process_alive(pid):
                return None
            return pid
        except Exception:  # noqa: BLE001 — observability only, never crash a wait
            return None

    def clear_waiting(self, agent: str) -> None:
        """Remove the waiting marker if present. Best-effort, never raises."""
        try:
            with self._waiting_lock(agent):
                self._unlink_waiting_locked(agent)
        except OSError:
            pass

    def clear_waiting_if_token(self, agent: str, wait_token: str) -> bool:
        """Remove the waiting marker only if it still belongs to ``wait_token``.

        This keeps an older waiter from erasing the marker for a newer waiter
        that superseded it. Observational only; returns whether it unlinked.
        """
        try:
            with self._waiting_lock(agent):
                marker = self.read_waiting(agent)
                if not marker or marker.get("wait_token") != wait_token:
                    return False
                return self._unlink_waiting_locked(agent)
        except OSError:
            return False

    def waiting_superseded(self, agent: str, wait_token: str) -> dict | None:
        """Return the supersession event for ``wait_token`` if a newer wait replaced it."""
        path = self._waiting_superseded_path(agent, wait_token)
        if path is None or not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8").strip()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def clear_waiting_superseded(self, agent: str, wait_token: str) -> None:
        """Remove a supersession event after the superseded waiter observes it."""
        path = self._waiting_superseded_path(agent, wait_token)
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # ------------------------------------------ wrapped reply-await markers

    @property
    def awaiting_dir(self) -> Path:
        """Token-keyed observational waits created by wrapped ``--await-reply``.

        These records never affect delivery, cursors, supervisor actions, or
        thread closure.  They only prove that a wrapped turn explicitly chose
        to wait across turns for one outbound request.
        """
        return self.state_dir / "awaiting"

    @staticmethod
    def _valid_await_record(raw: object) -> dict | None:
        if not isinstance(raw, dict) or frozenset(raw) != _AWAIT_FIELDS:
            return None
        if raw.get("schema_version") != _AWAIT_SCHEMA_VERSION:
            return None
        try:
            validate_agent_name(raw.get("agent"))
        except (TypeError, ValueError):
            return None
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or _AWAIT_ID_RE.fullmatch(request_id) is None:
            return None
        for key in ("wrapper_generation", "wait_token"):
            value = raw.get(key)
            if not isinstance(value, str) or _AWAIT_PATH_TOKEN_RE.fullmatch(value) is None:
                return None
        if raw.get("source") not in _AWAIT_SOURCES:
            return None
        started_at = raw.get("started_at")
        if not isinstance(started_at, str):
            return None
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return dict(raw)

    def _awaiting_lock(self, agent: str):
        name = validate_agent_name(agent)
        return self._exclusive_lock(
            self.awaiting_dir / f"{name}.lock",
            what=f"wrapped awaiting marker lock for {name}",
        )

    def _awaiting_path(self, agent: str, generation: str, wait_token: str) -> Path:
        name = validate_agent_name(agent)
        if not isinstance(generation, str) or _AWAIT_PATH_TOKEN_RE.fullmatch(generation) is None:
            raise ValueError("unsafe wrapper generation")
        if not isinstance(wait_token, str) or _AWAIT_PATH_TOKEN_RE.fullmatch(wait_token) is None:
            raise ValueError("unsafe await token")
        return self.awaiting_dir / name / generation / f"{wait_token}.json"

    def write_awaiting(self, agent: str, record: dict) -> None:
        """Atomically persist one strict, body-free wrapped reply wait.

        The path is keyed by agent/generation/token so concurrent or superseded
        wrapper generations cannot overwrite one another.  A small per-agent
        retention cap bounds crash leftovers; active-state readers still require
        the current generation and validated open thread.
        """
        clean = self._valid_await_record(record)
        if clean is None or clean.get("agent") != agent:
            raise ValueError("invalid wrapped await record")
        path = self._awaiting_path(
            agent,
            clean["wrapper_generation"],
            clean["wait_token"],
        )
        with self._awaiting_lock(agent):
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, json.dumps(clean, ensure_ascii=False, sort_keys=True))
            files = sorted(
                (p for p in (self.awaiting_dir / agent).glob("*/*.json") if p.is_file()),
                key=lambda p: (p.stat().st_mtime_ns, str(p)),
            )
            for old in files[:-_AWAIT_MAX_RECORDS_PER_AGENT]:
                try:
                    old.unlink()
                except OSError:
                    pass

    def list_awaiting(self, agent: str | None = None) -> tuple[list[dict], list[dict]]:
        """Return strict wrapped-await records plus body-free diagnostics.

        Torn, oversized, path-mismatched, or forward-unknown records are skipped.
        The detector therefore fails quiet while doctor can report the bounded
        diagnostic.  This reader never raises.
        """
        records: list[dict] = []
        problems: list[dict] = []
        try:
            if agent:
                roots = [self.awaiting_dir / validate_agent_name(agent)]
            else:
                roots = []
                for index, path in enumerate(self.awaiting_dir.iterdir()):
                    if index >= _AWAIT_MAX_ROOT_ENTRIES:
                        problems.append({"code": "await_root_limit_reached"})
                        break
                    if path.is_dir():
                        roots.append(path)
                roots.sort()
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return records, problems
        for agent_dir in roots:
            try:
                path_agent = validate_agent_name(agent_dir.name)
            except ValueError:
                if len(problems) < _AWAIT_MAX_DIAGNOSTICS:
                    problems.append({
                        "code": "await_record_invalid",
                        "path": agent_dir.name,
                    })
                continue
            try:
                paths = []
                for index, path in enumerate(agent_dir.glob("*/*.json")):
                    if index >= _AWAIT_MAX_RECORDS_PER_AGENT:
                        if len(problems) < _AWAIT_MAX_DIAGNOSTICS:
                            problems.append({
                                "code": "await_record_limit_reached",
                                "path": path_agent,
                            })
                        break
                    paths.append(path)
                paths.sort()
            except OSError:
                if len(problems) < _AWAIT_MAX_DIAGNOSTICS:
                    problems.append({
                        "code": "await_record_unreadable",
                        "path": path_agent,
                    })
                continue
            for path in paths:
                rel = "/".join(path.relative_to(self.awaiting_dir).parts)
                try:
                    if path.stat().st_size > _AWAIT_MAX_RECORD_BYTES:
                        raise ValueError("oversized")
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    clean = self._valid_await_record(raw)
                    generation = path.parent.name
                    if (
                        clean is None
                        or clean.get("agent") != path_agent
                        or clean.get("wrapper_generation") != generation
                        or path.stem != clean.get("wait_token")
                    ):
                        raise ValueError("invalid")
                except (OSError, ValueError, json.JSONDecodeError):
                    if len(problems) < _AWAIT_MAX_DIAGNOSTICS:
                        problems.append({"code": "await_record_invalid", "path": rel})
                    continue
                records.append(clean)
        return records, problems

    def clear_awaiting_if_token(self, agent: str, wait_token: str) -> bool:
        """Remove only the wrapped-await record carrying ``wait_token``."""
        try:
            validate_agent_name(agent)
            if _AWAIT_PATH_TOKEN_RE.fullmatch(wait_token) is None:
                return False
            with self._awaiting_lock(agent):
                matches = list((self.awaiting_dir / agent).glob(f"*/{wait_token}.json"))
                if len(matches) != 1:
                    return False
                matches[0].unlink()
                return True
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False

    def clear_heartbeat(self, agent: str) -> None:
        """Remove the heartbeat marker if present. Best-effort, never raises. The
        wrapper uses this to FORCE-STALE a failed turn: a turn may stamp heartbeat
        on its streaming progress (so a long SUCCESSFUL turn stays live), but if the
        turn then fails (no completed boundary / nonzero exit), clearing ensures the
        failed attempt leaves no fresh heartbeat -> a persistently-failing agent
        goes stale -> the supervisor restarts it."""
        p = self.state_dir / f"{agent}.heartbeat"
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # ------------------------------------------------ work-heartbeat diagnostics

    def work_heartbeat_status_path(self, agent: str) -> Path:
        return self.state_dir / "work-heartbeat" / f"{validate_agent_name(agent)}.json"

    def write_work_heartbeat_status(self, agent: str, status: dict) -> None:
        """Best-effort DIAGNOSTICS record of the in-turn work-heartbeat ticker
        (disabled/active/stopped reason + stamp/error counts). NOT a supervisor
        input in v1 - doctor/status may read it to explain why a long silent turn
        went stale. Never raises (a diagnostics write must not fail a turn); lives
        in state_dir so ``reset`` clears it with the other liveness state."""
        try:
            p = self.work_heartbeat_status_path(agent)
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(p, json.dumps(status, ensure_ascii=False))
        except (OSError, ValueError, TypeError):
            pass

    def read_work_heartbeat_status(self, agent: str) -> dict | None:
        """The last work-heartbeat diagnostics record, or None if absent/corrupt."""
        try:
            p = self.work_heartbeat_status_path(agent)
        except ValueError:
            return None
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------ wrapper health

    def health_path(self, agent: str) -> Path:
        """Adjacent advisory health marker for a wrapped agent."""
        return self.state_dir / f"{agent}.health.json"

    def write_health(self, agent: str, snapshot: dict) -> None:
        """Atomically write ``state/<agent>.health.json``.

        Health is advisory only. The heartbeat remains the liveness authority.
        The schema itself is closed and redacted by ``agenttalk.health``; callers
        must pass only sanitized snapshots.
        """
        _atomic_write_text(
            self.health_path(agent),
            json.dumps(snapshot, indent=2, ensure_ascii=False),
        )

    def read_health_raw(self, agent: str) -> dict | None:
        """Return the raw health marker, or None if absent/corrupt/unreadable."""
        p = self.health_path(agent)
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def read_health(self, agent: str, *, now_epoch: float | None = None,
                    heartbeat: datetime | None = None,
                    ttl_seconds: float = _health.DEFAULT_TTL_SECONDS,
                    heartbeat_skew_seconds: float = _health.DEFAULT_HEARTBEAT_SKEW_SECONDS
                    ) -> dict:
        """Return a degrade-safe advisory health view.

        Missing, malformed, stale, or heartbeat-skewed health reads as
        ``state=unknown`` with a local warning. This never raises and never
        derives liveness authority.
        """
        raw = self.read_health_raw(agent)
        return _health.normalize(
            raw,
            agent=agent,
            now_epoch=now_epoch,
            heartbeat=heartbeat,
            ttl_seconds=ttl_seconds,
            heartbeat_skew_seconds=heartbeat_skew_seconds,
        )

    # ----------------------------------------- durable config-blocked hold
    #
    # A wrapper launch/runtime config_blocked failure exits before it can refresh
    # health. Health is advisory and TTL-degrades, so supervisor recovery authority
    # needs a durable hold marker that survives until operator repair/restart.

    def config_blocked_hold_path(self, agent: str) -> Path:
        return self.state_dir / "config-blocked-hold" / f"{validate_agent_name(agent)}.json"

    def write_config_blocked_hold(self, agent: str, *, summary: str = "") -> None:
        """Atomically record that ``agent`` is held on launch config repair."""
        payload = {
            "agent": validate_agent_name(agent),
            "state": "config_blocked",
            "summary": str(summary or ""),
            "at": _now_iso(),
        }
        with self._config_lock():
            p = self.config_blocked_hold_path(agent)
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_config_blocked_hold(self, agent: str) -> dict | None:
        """Return the validated config-blocked hold marker, or None if invalid."""
        expected = validate_agent_name(agent)
        p = self.config_blocked_hold_path(agent)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8").strip() or "null")
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("agent") != expected or data.get("state") != "config_blocked":
            return None
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        at = data.get("at") if isinstance(data.get("at"), str) else None
        return {
            "agent": expected,
            "state": "config_blocked",
            "summary": summary,
            "at": at,
        }

    def clear_config_blocked_hold(self, agent: str) -> None:
        """Remove the config-blocked hold marker (best-effort; never raises)."""
        with self._config_lock():
            try:
                self.config_blocked_hold_path(agent).unlink()
            except (FileNotFoundError, OSError):
                pass

    # ------------------------------------------- durable quota-blocked hold
    #
    # A provider quota/billing refusal (task #126) is a distinct terminal class
    # from ambiguous_or_unknown: retrying against a wall that cannot clear burns
    # attempts and dead-letters valid work. Unlike config_blocked (which sticky-
    # parks until an operator repairs/restarts), this hold SELF-EXPIRES once the
    # stated reset instant passes - `read_quota_blocked_hold` returns None past
    # that instant, with no explicit clear required, so the wrapper naturally
    # re-drives and the operator surfaces naturally stop showing it.

    def quota_blocked_hold_path(self, agent: str) -> Path:
        return self.state_dir / "quota-blocked-hold" / f"{validate_agent_name(agent)}.json"

    def write_quota_blocked_hold(self, agent: str, *, summary: str = "",
                                 reset_at: str | None = None) -> None:
        """Atomically record that ``agent`` is blocked on a provider quota/billing
        refusal. ``reset_at`` is the parsed reset instant (ISO UTC) when the
        provider's own text stated one, else None (still a hold, just with an
        unknown clear time - the caller falls back to bounded backoff)."""
        payload = {
            "agent": validate_agent_name(agent),
            "state": "quota_blocked",
            "summary": str(summary or ""),
            "reset_at": reset_at if isinstance(reset_at, str) else None,
            "at": _now_iso(),
        }
        with self._config_lock():
            p = self.quota_blocked_hold_path(agent)
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_quota_blocked_hold(self, agent: str, *,
                                now_epoch: float | None = None) -> dict | None:
        """Return the validated quota-blocked hold marker, or None if absent,
        invalid, or SELF-EXPIRED (``now_epoch`` at/after ``reset_at``) - the
        expiry check that lets a quota hold clear without operator action."""
        expected = validate_agent_name(agent)
        p = self.quota_blocked_hold_path(agent)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8").strip() or "null")
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("agent") != expected or data.get("state") != "quota_blocked":
            return None
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        at = data.get("at") if isinstance(data.get("at"), str) else None
        reset_at = data.get("reset_at") if isinstance(data.get("reset_at"), str) else None
        if reset_at is not None:
            from agenttalk.health import parse_iso
            parsed = parse_iso(reset_at)
            if parsed is None:
                reset_at = None  # torn/unparseable - keep the hold, drop the stale instant
            else:
                now = time.time() if now_epoch is None else float(now_epoch)
                if now >= parsed.timestamp():
                    return None  # self-expired: the reset instant has passed
        return {
            "agent": expected,
            "state": "quota_blocked",
            "summary": summary,
            "reset_at": reset_at,
            "at": at,
        }

    def clear_quota_blocked_hold(self, agent: str) -> None:
        """Remove the quota-blocked hold marker (best-effort; never raises)."""
        with self._config_lock():
            try:
                self.quota_blocked_hold_path(agent).unlink()
            except (FileNotFoundError, OSError):
                pass

    # ----------------------------------------- managed lead-loop (Slice 1)
    #
    # A managed lead-loop identity is a wrapped controller that OWNS its team
    # mailbox so it can never silently un-arm. Ownership is a renewable LEASE
    # (state/<agent>.lead-loop-lease.json) - the CORRECTNESS state. The .waiting
    # marker is only an observational MIRROR of the live lease (status/UX). Slice
    # 1 ships the lease mechanism + config + guard + visibility; the controller
    # that acquires/renews it is Slice 2. Everything keys off the AGENT NAME +
    # its managed_lead_loop config, NEVER the cli (a codex identity can be a
    # managed lead-loop exactly as a claude one can).

    def managed_lead_loop_agents(self) -> dict:
        """Return the ``{agent: {enabled, ttl_seconds, cadence_seconds}}`` map
        ({} if none configured). Read-only; degrade-safe."""
        return self.load_config().get("managed_lead_loop", {}) or {}

    def managed_lead_loop_spec(self, agent: str) -> dict | None:
        """Resolved spec for ``agent`` ({enabled, ttl_seconds, cadence_seconds}
        with defaults filled), or None if not configured. ``enabled`` defaults
        True so a bare ``{}`` entry means 'managed with default bounds'."""
        spec = self.managed_lead_loop_agents().get(agent)
        if not isinstance(spec, dict):
            return None
        return {
            "enabled": bool(spec.get("enabled", True)),
            "ttl_seconds": float(spec.get("ttl_seconds", LEAD_LOOP_TTL_DEFAULT)),
            "cadence_seconds": float(spec.get("cadence_seconds", LEAD_LOOP_CADENCE_DEFAULT)),
        }

    def is_managed_lead_loop(self, agent: str) -> bool:
        """True iff ``agent`` is configured AND enabled as a managed lead-loop."""
        spec = self.managed_lead_loop_spec(agent)
        return bool(spec and spec["enabled"])

    def set_managed_lead_loop(self, agent: str, *, enabled: bool = True,
                              ttl_seconds: float | None = None,
                              cadence_seconds: float | None = None) -> None:
        """Mark ``agent`` as a managed lead-loop (or clear it with enabled=False).
        Config write under the shared lock (like set_role); validated fail-closed.
        CLEARING (enabled=False) also FORCE-RELEASES any live lease + mirror so the
        now-unmanaged identity is not left guarded / un-stealable (reviewer-1)."""
        with self._config_lock():
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
            if agent not in roster:
                raise ValueError(f"agent {agent!r} is not in the roster {sorted(roster)}")
            managed = self._cfg_dict(cfg, "managed_lead_loop")
            if enabled:
                managed[agent] = {
                    "enabled": True,
                    "ttl_seconds": float(ttl_seconds if ttl_seconds is not None
                                         else LEAD_LOOP_TTL_DEFAULT),
                    "cadence_seconds": float(cadence_seconds if cadence_seconds is not None
                                             else LEAD_LOOP_CADENCE_DEFAULT),
                }
            else:
                managed.pop(agent, None)
            validate_managed_lead_loop(managed, roster)
            self._write_config(cfg)
        # Outside the config lock (the lease has its own lock): unmanaging an agent
        # force-releases its lease so it is not left guarded/un-stealable.
        if not enabled:
            self.release_lead_loop_lease(agent)

    def lead_loop_lease_path(self, agent: str):
        return self.state_dir / f"{validate_agent_name(agent)}.lead-loop-lease.json"

    # --- managed lead-loop CONTROLLER exit marker (WP2) -----------------------
    #
    # The wrapped controller records WHY it exited so the supervisor can tell a
    # deliberate non-crash exit (do NOT relaunch) from a crash (relaunch + recover):
    #   * ``blocked``     - acquire found another LIVE owner; this duplicate stood
    #     down without ever owning the lease (supervisor HOLD, no relaunch).
    #   * ``stood_down``  - a VALID human release/end (v0.39 authority); the
    #     stand-down must STICK against auto_restart (supervisor NO relaunch until an
    #     operator request-launch re-arms).
    # A crash/exception/SIGKILL writes NO marker -> the supervisor relaunches as
    # usual. The marker lives in state/ (cleared by reset like the lease) and is
    # cleared when a controller next ACQUIRES (a live controller makes any prior
    # exit state moot). Degrade-safe: a torn/absent marker reads as None.
    LEAD_LOOP_EXIT_BLOCKED = "blocked"
    LEAD_LOOP_EXIT_STOOD_DOWN = "stood_down"

    def lead_loop_exit_path(self, agent: str):
        return self.state_dir / f"{validate_agent_name(agent)}.lead-loop-exit.json"

    def write_lead_loop_exit(self, agent: str, *, state: str,
                             owner_pid: int | None = None, reason: str = "") -> None:
        """Record the controller's exit reason (atomic). ``state`` is one of
        ``LEAD_LOOP_EXIT_BLOCKED`` / ``LEAD_LOOP_EXIT_STOOD_DOWN``."""
        _atomic_write_text(self.lead_loop_exit_path(agent), json.dumps({
            "agent": agent, "state": state,
            "owner_pid": owner_pid if isinstance(owner_pid, int) else None,
            "reason": str(reason or ""), "at": _now_iso(),
        }, ensure_ascii=False, indent=2))

    def read_lead_loop_exit(self, agent: str) -> dict | None:
        """The parsed exit marker, or None if absent/corrupt (never raises)."""
        p = self.lead_loop_exit_path(agent)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8").strip() or "null")
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def clear_lead_loop_exit(self, agent: str) -> None:
        """Remove the exit marker (best-effort; never raises)."""
        try:
            self.lead_loop_exit_path(agent).unlink()
        except (FileNotFoundError, OSError):
            pass

    # --- managed lead-loop CADENCE STATE (WP3) ---------------------------------
    # Controller-owned SINGLE-WRITER per-agent state for the proactive cadence
    # sweep: when the last sweep ran (drives due-ness), the reminder dedup map
    # ((request_id -> last_msg_id) so an open-outbound nudge fires once per thread
    # state), the dead-letter / unrouted-escalation dedup set, and the
    # failure/backoff fields (a failed tick is controller-HEALTH, not poison: it
    # backs off and, past a threshold, escalates once). Lives in state_dir so
    # reset() clears it; the dead-letter SINK is elsewhere and survives reset.

    def lead_loop_cadence_path(self, agent: str) -> Path:
        return self.state_dir / f"{validate_agent_name(agent)}.lead-loop-cadence.json"

    def _default_cadence_state(self) -> dict:
        return {"last_tick_epoch": 0.0, "last_reminded": {}, "escalation_dedup": {},
                "cadence_fails": 0, "backoff_until_epoch": 0.0, "health_escalated": False}

    def read_lead_loop_cadence(self, agent: str) -> dict:
        """Return the cadence state, DEGRADE-SAFE: a missing / empty / torn / corrupt /
        forward-incompatible file reads as the fresh default (never raises). Every field
        is coerced to its expected type so a hand-edited value errs SAFE rather than
        crashing the controller's idle loop (mirrors ``_safe_int`` in the wrapper loop)."""
        d = self._default_cadence_state()
        p = self.lead_loop_cadence_path(agent)
        if not p.exists():
            return d
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return d
        if not raw:
            return d
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return d
        if not isinstance(data, dict):
            return d

        def _f(key: str) -> float:
            try:
                return float(data.get(key))
            except (TypeError, ValueError):
                return 0.0

        def _i(key: str) -> int:
            try:
                return int(data.get(key))
            except (TypeError, ValueError):
                return 0

        lr = data.get("last_reminded")
        ed = data.get("escalation_dedup")
        return {
            "last_tick_epoch": _f("last_tick_epoch"),
            "last_reminded": lr if isinstance(lr, dict) else {},
            "escalation_dedup": ed if isinstance(ed, dict) else {},
            "cadence_fails": _i("cadence_fails"),
            "backoff_until_epoch": _f("backoff_until_epoch"),
            "health_escalated": bool(data.get("health_escalated")),
        }

    def write_lead_loop_cadence(self, agent: str, state: dict) -> None:
        """Atomically persist the cadence state. Single-writer (the lease-owning
        controller is the only writer), so no lock is needed; the atomic replace keeps
        a concurrent reader from seeing a torn file."""
        _atomic_write_text(self.lead_loop_cadence_path(agent),
                           json.dumps(state, indent=2))

    def _lead_loop_lease_lock(self, agent: str):
        """Exclusive per-agent lock serializing acquire/renew/release/steal so the
        read-decide-write is ATOMIC - two contenders can never both 'acquire' an
        empty lease (reviewer-1 blocker). Reuses the cross-platform OS lock."""
        return self._exclusive_lock(
            self.state_dir / f"{validate_agent_name(agent)}.lead-loop-lease.lock",
            what="lead-loop lease lock")

    def read_lead_loop_lease(self, agent: str) -> dict | None:
        """Return the parsed lease dict, or None if absent/corrupt. Never raises
        (a torn write reads as None -> treated as 'no lease', fail-safe).

        NORMALIZES ``expires_at`` at the read boundary (WP1): it is coerced to a
        finite float, or None when missing / non-numeric / NaN / +-inf. Every
        consumer then sees one shape, and ``_lease_expired`` treats None as
        fail-safe NOT-expired (a garbage expiry never displaces or false-ERRORs a
        maybe-live owner). This is the SINGLE place expiry is sanitized."""
        p = self.lead_loop_lease_path(agent)
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        exp = data.get("expires_at")
        data["expires_at"] = (
            float(exp)
            if isinstance(exp, (int, float)) and not isinstance(exp, bool)
            and math.isfinite(exp)
            else None
        )
        return data

    def _write_lead_loop_lease(self, agent: str, lease: dict) -> None:
        """Atomically write the lease (the correctness state), then BEST-EFFORT
        mirror it into .waiting (observational). The two writes are NOT atomically
        coupled - a crash between them leaves the lease without a mirror, which is
        fine: the mirror is observational-only and a lease without a mirror is still
        valid (readers degrade). The mirror lets status/threads/agent_active see the
        controller as armed; it carries pid + deadline_epoch (existing readers) plus
        a ``lead_loop`` flag. It deliberately does NOT carry the lease_id: status
        returns the .waiting object verbatim, so mirroring the lease_id there would
        LEAK the guard's owner-bypass token to any read-only inspector (reviewer-1
        blocker). Callers hold lease_id from acquire()'s return value instead."""
        _atomic_write_text(self.lead_loop_lease_path(agent),
                           json.dumps(lease, ensure_ascii=False, indent=2))
        self.write_waiting(agent, {
            "agent": agent,
            "pid": lease.get("owner_pid"),
            "since": lease.get("acquired_at"),
            "deadline_epoch": lease.get("expires_at"),
            "wait_token": lease.get("wrapper_generation"),
            "wrapper_generation": lease.get("wrapper_generation"),
            "lead_loop": True,
            "managed": True,
        })

    @staticmethod
    def _lease_expired(lease: dict, now: float) -> bool:
        """True only when the lease has a FINITE expiry that ``now`` is past.
        ``read_lead_loop_lease`` normalizes ``expires_at`` to a finite float or None,
        so a missing/garbage expiry is None here -> fail-safe NOT-expired (a garbage
        expiry never displaces or false-ERRORs a maybe-live owner). WP1 single
        expiry predicate shared by the authority."""
        exp = lease.get("expires_at")
        return isinstance(exp, (int, float)) and not isinstance(exp, bool) and now > exp

    def _heartbeat_stale(self, agent: str, now: float, stale_after: float) -> bool:
        """True when ``agent`` has no heartbeat, or its heartbeat is older than
        ``stale_after`` seconds or beyond allowed future skew. WP1 single
        heartbeat-staleness predicate."""
        hb = self.read_heartbeat(agent)
        age = self._bounded_heartbeat_age_seconds(hb, now)
        return age is None or age > stale_after

    def _lead_loop_authority(self, agent: str, lease: dict | None, *, now: float,
                             heartbeat_stale_after: float) -> dict:
        """THE single source of truth for every lead-loop AUTHORITY decision (WP1).
        ``_lease_stealable``, ``lead_loop_state``, AND ``lead_loop_active_owner`` all
        derive from this one dict - no caller computes its own liveness/expiry branch,
        so steal, armed, and guard can NEVER disagree (the drift bug class that bit
        this surface twice). Returns {managed, present, owner_liveness, owner_alive,
        expired, heartbeat_stale, stealable, armed, guarded, reason}.

        For a present MANAGED lease, by construction:
          - guarded   = owner liveness is NOT confirmed-dead (ALIVE and UNKNOWN both
                        guard - an uncertain probe is probably-alive, so an external
                        consumer never races a possibly-live controller);
          - stealable = CONFIRMED-dead (immediate recovery, no TTL wait) OR
                        (expired AND heartbeat-stale) for an ALIVE/UNKNOWN owner;
          - armed     = NOT stealable (the EXACT complement, for every case).
        An UNMANAGED stray lease is INERT (config-gated): managed/guarded/armed all
        False, stealable False - never auto-stolen, never reported armed."""
        managed = self.is_managed_lead_loop(agent)
        present = lease is not None
        out = {
            "managed": managed, "present": present,
            "owner_liveness": None, "owner_alive": False,
            "expired": None, "heartbeat_stale": None,
            "stealable": False, "armed": False, "guarded": False, "reason": "",
        }
        if not present:
            out["reason"] = "no lease"
            return out
        if not managed:
            out["reason"] = "not managed"  # stray lease for a manual identity -> inert
            return out
        liveness = _process_liveness(lease.get("owner_pid"))
        expired = self._lease_expired(lease, now)
        hb_stale = self._heartbeat_stale(agent, now, heartbeat_stale_after)
        out["owner_liveness"] = liveness
        out["owner_alive"] = liveness == PROC_ALIVE
        out["expired"] = expired
        out["heartbeat_stale"] = hb_stale
        out["guarded"] = liveness != PROC_DEAD
        out["stealable"] = (liveness == PROC_DEAD) or (expired and hb_stale)
        out["armed"] = not out["stealable"]
        if liveness == PROC_DEAD:
            out["reason"] = "owner confirmed dead"
        elif out["stealable"]:
            out["reason"] = "lease expired and heartbeat stale"
        elif liveness == PROC_UNKNOWN:
            out["reason"] = "armed (owner liveness unknown, treated as alive)"
        elif expired:
            out["reason"] = "armed (lease expired, heartbeat fresh, pending renewal)"
        elif hb_stale:
            out["reason"] = "armed (heartbeat stale, lease within TTL)"
        else:
            out["reason"] = "armed"
        return out

    def _lease_stealable(self, existing: dict, agent: str, *, now: float,
                         heartbeat_stale_after: float | None) -> bool:
        """Whether a managed lease may be STOLEN now. Thin wrapper over the single
        :meth:`_lead_loop_authority` (WP1) so steal can never drift from armed/guard:
        a CONFIRMED-dead owner is stealable immediately (no TTL wait); an ALIVE or
        UNKNOWN owner only once the lease is EXPIRED *and* heartbeat-stale; an
        unmanaged stray lease is never auto-stolen."""
        stale_after = (heartbeat_stale_after if heartbeat_stale_after is not None
                       else ACTIVE_WITHIN_SECONDS)
        return self._lead_loop_authority(
            agent, existing, now=now, heartbeat_stale_after=stale_after)["stealable"]

    def acquire_lead_loop_lease(self, agent: str, *, owner_pid: int,
                                ttl_seconds: float | None = None,
                                now: float | None = None,
                                session_id: str | None = None,
                                lease_id: str | None = None,
                                wrapper_generation: str | None = None,
                                heartbeat_stale_after: float | None = None) -> dict | None:
        """Acquire (or re-acquire / steal) the lease. Returns the lease dict on
        success, or None when a live lease held by ANOTHER owner is not stealable
        (the caller is blocked). Re-acquiring with a matching lease_id refreshes.
        ATOMIC under the per-agent lease lock: the read-decide-write is serialized so
        two contenders can never both acquire an empty lease (reviewer-1 blocker)."""
        now = now if now is not None else time.time()
        ttl = float(ttl_seconds) if ttl_seconds is not None else LEAD_LOOP_TTL_DEFAULT
        with self._lead_loop_lease_lock(agent):
            existing = self.read_lead_loop_lease(agent)
            if existing:
                same_owner = lease_id is not None and existing.get("lease_id") == lease_id
                if not same_owner and not self._lease_stealable(
                        existing, agent, now=now,
                        heartbeat_stale_after=heartbeat_stale_after):
                    return None  # a live, non-stealable lease held by another owner
            lid = lease_id or uuid.uuid4().hex
            iso = _now_iso()
            keep_acquired = (existing.get("acquired_at")
                             if existing and existing.get("lease_id") == lid else iso)
            keep_start = (existing.get("owner_start")
                          if existing and existing.get("lease_id") == lid else iso)
            if existing and existing.get("lease_id") == lid:
                keep_generation = (
                    existing.get("wrapper_generation")
                    or wrapper_generation
                    or uuid.uuid4().hex
                )
            else:
                keep_generation = wrapper_generation or uuid.uuid4().hex
            lease = {
                "schema_version": 1, "managed": True, "mode": LEAD_LOOP_MODE,
                "agent": agent, "owner_pid": int(owner_pid), "owner_start": keep_start,
                "session_id": session_id, "lease_id": lid,
                "wrapper_generation": keep_generation,
                "acquired_at": keep_acquired, "renewed_at": iso,
                "expires_at": float(now) + ttl,
            }
            self._write_lead_loop_lease(agent, lease)
            return lease

    def renew_lead_loop_lease(self, agent: str, *, lease_id: str,
                              ttl_seconds: float | None = None,
                              now: float | None = None) -> dict | None:
        """Extend the lease iff the caller owns it (lease_id matches). Returns the
        updated lease, or None if there is no lease or the caller is not the owner.
        Atomic under the per-agent lease lock."""
        now = now if now is not None else time.time()
        ttl = float(ttl_seconds) if ttl_seconds is not None else LEAD_LOOP_TTL_DEFAULT
        with self._lead_loop_lease_lock(agent):
            existing = self.read_lead_loop_lease(agent)
            if not existing or existing.get("lease_id") != lease_id:
                return None
            existing["renewed_at"] = _now_iso()
            existing["expires_at"] = float(now) + ttl
            self._write_lead_loop_lease(agent, existing)
            return existing

    def release_lead_loop_lease(self, agent: str, *, lease_id: str | None = None) -> bool:
        """Release the lease. With a lease_id, releases only iff the caller owns it
        (returns False otherwise); with lease_id=None, force-releases (recovery, e.g.
        when an agent is un-managed). Atomic under the per-agent lease lock. Clears
        the .waiting mirror when it is a lead-loop mirror (the mirror no longer
        carries a lease_id, so it is matched by the lead_loop flag - safe under the
        lock, which serializes release vs a concurrent acquire's mirror write)."""
        with self._lead_loop_lease_lock(agent):
            existing = self.read_lead_loop_lease(agent)
            if existing and lease_id is not None and existing.get("lease_id") != lease_id:
                return False
            try:
                self.lead_loop_lease_path(agent).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
            mirror = self.read_waiting(agent)
            if isinstance(mirror, dict) and mirror.get("lead_loop"):
                self.clear_waiting(agent)
            return True

    def lead_loop_active_owner(self, agent: str, *, now: float | None = None,
                               heartbeat_stale_after: float | None = None) -> dict | None:
        """Return the lease iff it GUARDS the mailbox (the single-consumer guard's
        'is the mailbox owned' test), else None. Derives ``guarded`` from the single
        :meth:`_lead_loop_authority` (WP1): a present managed lease guards unless the
        owner is CONFIRMED dead - ALIVE *and* UNKNOWN both guard (an uncertain probe
        is probably-alive, so an external consumer never races a possibly-live
        controller); a confirmed-dead owner yields None (orphaned -> recoverable). A
        stray lease for an UNMANAGED identity never guards (config-gated). Independent
        of expiry. Uses the same authority as steal/armed so the three never disagree."""
        now = now if now is not None else time.time()
        stale_after = (heartbeat_stale_after if heartbeat_stale_after is not None
                       else ACTIVE_WITHIN_SECONDS)
        lease = self.read_lead_loop_lease(agent)
        if not lease:
            return None
        auth = self._lead_loop_authority(
            agent, lease, now=now, heartbeat_stale_after=stale_after)
        return lease if auth["guarded"] else None

    def lead_loop_state(self, agent: str, *, now: float | None = None,
                        heartbeat_stale_after: float | None = None) -> dict:
        """Visibility snapshot for ``agent`` (status/doctor/supervisor). Returns
        {managed, present, owner_pid, owner_alive, owner_liveness, expired,
        heartbeat_stale, armed, reason}, ALL derived from the single
        :meth:`_lead_loop_authority` (WP1) so the detector can never disagree with
        steal/guard. ``armed`` (managed health) = a present managed lease that is NOT
        stealable, i.e. NOT confirmed-dead AND NOT (expired AND heartbeat-stale). A
        healthy long turn (within TTL) is armed; an expired-but-heartbeating owner is
        armed (it renews on its next cadence); a within-TTL owner whose heartbeat
        merely lapsed is armed; an UNKNOWN-liveness owner (uncertain probe) is treated
        as probably-alive -> armed within TTL (never a false unarmed from a fail-quiet
        probe - codex blocker / lead D-12 Option A). Only a confirmed-dead owner or the
        both-stale case is unarmed; an UNMANAGED stray lease is present-but-not-armed
        (reason 'not managed'). ``owner_alive`` is True only for a CONFIRMED-live
        probe; ``owner_liveness`` carries the raw tri-state (alive/dead/unknown)."""
        now = now if now is not None else time.time()
        stale_after = (heartbeat_stale_after if heartbeat_stale_after is not None
                       else ACTIVE_WITHIN_SECONDS)
        lease = self.read_lead_loop_lease(agent)
        auth = self._lead_loop_authority(
            agent, lease, now=now, heartbeat_stale_after=stale_after)
        return {
            "managed": auth["managed"],
            "present": auth["present"],
            "owner_pid": lease.get("owner_pid") if lease else None,
            "owner_alive": auth["owner_alive"],
            "owner_liveness": auth["owner_liveness"],
            "expired": auth["expired"],
            "heartbeat_stale": auth["heartbeat_stale"],
            "armed": auth["armed"],
            "reason": auth["reason"],
        }

    def clear_dead_waiter(self, agent: str, self_pid: int) -> bool:
        """Remove ``agent``'s waiting marker iff it is owned by a CONFIRMED-DEAD
        other process (reap fix #4b). Returns True when it cleared one.

        Cosmetic crash-recovery: a wait that died without running its
        ``finally`` leaves a ghost ``.waiting`` marker that makes ``status``
        report a waiter that no longer exists. A *fresh* wait arming as the
        same agent calls this so the ghost is removed rather than merely
        overwritten (which already happens, but only for the same agent).
        Never touches a LIVE owner (that is the duplicate-activation case,
        handled separately) or our own pid. Best-effort, never raises.
        """
        try:
            with self._waiting_lock(agent):
                marker = self.read_waiting(agent)
                if not marker:
                    return False
                pid = marker.get("pid")
                if not isinstance(pid, int) or pid == self_pid:
                    return False
                if _process_alive(pid):
                    return False
                return self._unlink_waiting_locked(agent)
        except Exception:  # noqa: BLE001 — observability only, never crash a wait
            return False

    def live_waiter_count(self, *, now: float | None = None,
                          stale_after: float | None = None) -> int:
        """Number of agents with a FRESH, LIVE ``.waiting`` marker (soft-cap
        signal, fix #4c). Counts every live waiter including the caller.

        Same freshness gate as ``foreign_wait_pid`` (not stale past
        ``deadline_epoch + stale_after``, owner pid alive) but across the
        whole state dir and without the ``pid != self`` filter — the caller
        warns when this exceeds a soft threshold so leftover poll loops from
        old sessions get noticed. Read-only, best-effort, never raises.
        """
        if now is None:
            now = time.time()
        if stale_after is None:
            stale_after = _WAIT_STALE_AFTER_DEFAULT
        count = 0
        try:
            if not self.state_dir.is_dir():
                return 0
            for p in self.state_dir.iterdir():
                if p.suffix != ".waiting":
                    continue
                marker = self.read_waiting(p.stem)
                if not marker:
                    continue
                pid = marker.get("pid")
                if not isinstance(pid, int):
                    continue
                deadline = marker.get("deadline_epoch")
                if isinstance(deadline, (int, float)) and now > deadline + stale_after:
                    continue
                if not _process_alive(pid):
                    continue
                count += 1
        except OSError:
            return count
        return count

    # ----------------------------------------------- unique-name self-join guard

    def agent_active(self, name: str, *, now: float | None = None) -> bool:
        """Is this identity currently IN USE? True when the agent's heartbeat is
        fresher than ``ACTIVE_WITHIN_SECONDS`` OR it has a FRESH, live ``.waiting``
        marker (owner pid alive AND not past ``deadline_epoch + stale_after``, the
        same freshness gate ``live_waiter_count`` uses - codex-reviewer-1 r1, so a
        long-expired marker whose pid was reused does not false-positive). The OR
        matters: a listener parked in ``wait`` has a marker even with NO activity
        hook (no heartbeat), while a busy agent has a heartbeat but (the zombie-wait
        insight) no waiter. The ``name`` is VALIDATED before any state-file read
        (an unsafe name can't be a real active identity and must never be
        interpolated into a path - codex-reviewer-1 r1). Never raises."""
        try:
            validate_agent_name(name)
        except ValueError:
            return False
        if now is None:
            now = time.time()
        hb = self.read_heartbeat(name)
        heartbeat_age = self._bounded_heartbeat_age_seconds(hb, now)
        if heartbeat_age is not None and heartbeat_age <= ACTIVE_WITHIN_SECONDS:
            return True
        marker = self.read_waiting(name)
        if isinstance(marker, dict):
            pid = marker.get("pid")
            deadline = marker.get("deadline_epoch")
            stale = (isinstance(deadline, (int, float))
                     and now > deadline + _WAIT_STALE_AFTER_DEFAULT)
            if isinstance(pid, int) and not stale and _process_alive(pid):
                return True
        return False

    def suggest_unique_name(self, base: str, *, now: float | None = None,
                            limit: int = 1000) -> str:
        """The first free ``<base>-N`` (N>=2) that is a VALID identifier AND
        neither a current roster member, an active identity, nor a retired
        tombstone - so a joining agent can ALWAYS adopt the suggestion without
        colliding or failing validation. The base is TRUNCATED so the suffix
        keeps the result within the 64-char limit (codex-reviewer-1 r1: an
        unbounded ``<base>-N`` could exceed the validator and be unadoptable)."""
        if now is None:
            now = time.time()
        cfg = self.load_config()
        roster = {a.casefold() for a in cfg.get("agents", []) or []}
        retired = {r.casefold() for r in self._retired_names(cfg)}
        for n in range(2, limit + 1):
            suffix = f"-{n}"
            cand = base[:max(1, 64 - len(suffix))] + suffix
            key = cand.casefold()
            if key in roster or key in retired:
                continue
            try:
                validate_agent_name(cand)
            except ValueError:
                continue
            if self.agent_active(cand, now=now):
                continue
            return cand
        # Exhausted (pathological): a bounded, valid last resort.
        return (base[:max(1, 64 - len(f"-{limit + 1}"))] + f"-{limit + 1}")

    # ----------------------------------------------------- compaction (#2)
    #
    # `compact` archives a contiguous PREFIX of VALID messages (id <
    # keep_floor) into archived/compacted/ — COLD storage, never read back,
    # so a moved message is invisible to every live derivation. The keep_floor
    # POLICY lives in the CLI (it needs thread derivation, and threads.py
    # imports Store, so Store must not import it back). Store only provides the
    # safe mover + counters + the throttle stamp; correctness rides entirely on
    # the caller passing a sound keep_floor.

    @property
    def compacted_dir(self) -> Path:
        """Cold destination for compacted messages. A sibling of the
        reset-archive session dirs, NOT one of them — per-message
        compaction must never collide with `reset --archive`'s wholesale
        ``archived/<session_id>/`` moves."""
        return self.dir / "archived" / "compacted"

    def live_message_count(self) -> int:
        """Count ``*.json`` files in messages/ (cheap readdir, no parse). The
        auto-compaction threshold proxy — an over-count from invalid files is
        harmless for a trigger gate."""
        d = self.messages_dir
        if not d.is_dir():
            return 0
        try:
            return sum(1 for p in d.iterdir() if p.suffix == ".json")
        except OSError:
            return 0

    def archive_messages_below(self, keep_floor: str, *,
                               dry_run: bool = False) -> list[dict]:
        """Move every VALID message with ``id < keep_floor`` into
        archived/compacted/. Returns ``[{"id","from","to"}]`` per file (the
        plan, when ``dry_run``).

        Safety contract (the whole point of WP-B):
        - Only DELIVERY-valid messages are moved. Selection is the structural
          scan MINUS every path the full delivery gate
          (``_invalid_file_entries`` — parse + schema + roster + HMAC) rejects,
          so a parse-valid-but-off-roster or bad/missing-signature file is
          NEVER archived and stays visible to status/doctor/prune. (Structural
          validity alone is NOT enough — a roster/HMAC-invalid file parses
          cleanly but must remain reportable as tamper.)
        - ``keep_floor`` falsy ("") is a no-op (a fail-safe fired upstream).
        - Per-file ``shutil.move`` (atomic rename); a collision in the cold
          dir is timestamp-suffixed, never overwritten (the quarantine /
          ``_archive_session`` precedent). Partial progress is safe and the
          caller recomputes ``keep_floor`` each run, so a crashed run is
          simply re-runnable — never cumulatively wrong.
        """
        if not keep_floor:
            return []
        valid_p, _ = self._scan_messages_with_paths()  # structural pass
        # Exclude everything the FULL delivery gate rejects (roster + HMAC on
        # top of parse/schema) so tamper stays live-visible, not silently cold.
        invalid_names = {p.name for p, _, _ in self._invalid_file_entries()}
        records: list[dict] = []
        made_dir = False
        for m, src in valid_p:
            if src.name in invalid_names:
                continue
            if m.id >= keep_floor:
                continue
            dst = self.compacted_dir / src.name
            record = {"id": m.id, "from": str(src), "to": str(dst)}
            if not dry_run:
                if not made_dir:
                    self.compacted_dir.mkdir(parents=True, exist_ok=True)
                    made_dir = True
                if dst.exists():
                    dst = self.compacted_dir / (
                        f"{src.name}.{_now_iso().replace(':', '-')}")
                    record["to"] = str(dst)
                shutil.move(str(src), str(dst))
            records.append(record)
        return records

    def read_compact_stamp(self) -> dict | None:
        """Last auto-compaction record (throttle gate). None if absent/corrupt."""
        p = self.state_dir / "compact.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def write_compact_stamp(self, payload: dict) -> None:
        """Best-effort throttle/audit stamp for the last compaction run."""
        try:
            _atomic_write_text(self.state_dir / "compact.json",
                               json.dumps(payload, ensure_ascii=False))
        except OSError:
            pass

    # ------------------------------------------- restart-request markers (#WP-2)
    #
    # A `state/<agent>.restart-request` marker is the MANUAL trigger for the
    # external supervisor: `agenttalk request-restart --for <agent>` writes it
    # atomically; the supervisor watches, relaunches, and clears it BY
    # request_id (so a marker rewritten after the relaunch decision is not lost
    # — never silently drop a failed request). Bus-side protocol; the
    # supervisor's own pid/backoff state stays in a script-local file, not here.

    def write_restart_request(self, agent: str, payload: dict) -> None:
        """Atomically write ``agent``'s restart-request marker UNDER the config lock, so
        a concurrent ``clear_restart_request`` cannot interleave between its read and
        unlink and drop a newer marker (C5b TOCTOU; mirrors archive_launch_request)."""
        with self._config_lock():
            _atomic_write_text(self.state_dir / f"{agent}.restart-request",
                               json.dumps(payload, ensure_ascii=False))

    def read_restart_request(self, agent: str) -> dict | None:
        """Return ``agent``'s restart-request marker, or None if absent/corrupt."""
        p = self.state_dir / f"{agent}.restart-request"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def clear_restart_request(self, agent: str, request_id: str) -> bool:
        """Clear ``agent``'s restart-request marker ONLY if its current
        ``request_id`` matches — so a NEWER request written after the relaunch
        decision survives (no lost-wakeup). Returns True when it cleared one.
        Best-effort, never raises.

        C5b: the read/compare/unlink runs UNDER the config lock so a concurrent
        ``write_restart_request`` cannot replace the marker between the compare and the
        unlink (a stale clearer must never remove a newer request)."""
        with self._config_lock():
            marker = self.read_restart_request(agent)
            if not marker or marker.get("request_id") != request_id:
                return False
            try:
                (self.state_dir / f"{agent}.restart-request").unlink()
                return True
            except OSError:
                return False

    # ------------------------------------------- launch-request markers
    #
    # Evidence-only ephemeral reviewers are queued by data-only markers under
    # state/launch-requests/<request_id>.json. The supervisor claims exactly one
    # queued marker, launches a one-shot temporary identity, then archives the
    # marker by request_id. The archive is audit, not active state.

    @property
    def launch_requests_dir(self) -> Path:
        return self.state_dir / "launch-requests"

    @property
    def launch_requests_archive_dir(self) -> Path:
        return self.launch_requests_dir / "archive"

    def _launch_request_path(self, request_id: str) -> Path:
        from agenttalk import ephemeral as _eph
        if not _eph.is_safe_id(request_id):
            raise ValueError(f"unsafe launch request_id {request_id!r}")
        return self.launch_requests_dir / f"{request_id}.json"

    def write_launch_request(self, payload: dict) -> None:
        """Create one launch request without overwriting an existing id."""
        from agenttalk import ephemeral as _eph
        rid = payload.get("request_id") if isinstance(payload, dict) else None
        if not _eph.is_safe_id(rid):
            raise ValueError(f"unsafe launch request_id {rid!r}")
        data = dict(payload)
        data.setdefault("state", _eph.STATE_QUEUED)
        self._launch_state_rank(data["state"])
        with self._retirement_lock(), self._config_lock():
            path = self._launch_request_path(rid)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _publish_text_no_replace(
                    path,
                    json.dumps(data, indent=2, ensure_ascii=False),
                    prepare_token=uuid.uuid4().hex,
                )
            except FileExistsError:
                raise ValueError(f"launch request {rid!r} already exists") from None

    def read_launch_request(self, request_id: str) -> dict | None:
        """Return one launch-request marker, or None if absent/corrupt."""
        try:
            p = self._launch_request_path(request_id)
        except ValueError:
            return None
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        if not isinstance(data, dict) or data.get("request_id") != request_id:
            return None
        return data

    def list_launch_requests(self) -> list[dict]:
        """List active launch-request markers. Corrupt files are ignored; doctor
        style reporting can grow later without making the supervisor brittle."""
        d = self.launch_requests_dir
        if not d.exists():
            return []
        out: list[dict] = []
        for p in sorted(d.iterdir()):
            if p.is_dir() or p.suffix != ".json":
                continue
            rid = p.stem
            data = self.read_launch_request(rid)
            if data is not None:
                out.append(data)
        return out

    def claim_launch_request(self, request_id: str, *, claimed_by: str,
                             at_epoch: float) -> dict | None:
        """Atomically move a queued launch request into claimed state.

        Returns the updated marker. Returns None when the marker is absent,
        already claimed/terminal, or superseded by a different request_id.
        """
        from agenttalk import ephemeral as _eph
        with self._config_lock():
            marker = self.read_launch_request(request_id)
            if not marker or marker.get("state", _eph.STATE_QUEUED) != _eph.STATE_QUEUED:
                return None
            marker["state"] = _eph.STATE_CLAIMED
            marker["claimed_by"] = claimed_by
            marker["claimed_at"] = _now_iso()
            marker["claimed_at_epoch"] = at_epoch
            _atomic_write_text(self._launch_request_path(request_id),
                               json.dumps(marker, indent=2, ensure_ascii=False))
            return marker

    @staticmethod
    def _launch_state_rank(state: object) -> int:
        from agenttalk import ephemeral as _eph
        if not isinstance(state, str):
            raise ValueError(f"invalid launch request state {state!r}")
        active = (
            _eph.STATE_QUEUED,
            _eph.STATE_CLAIMED,
            _eph.STATE_REQUESTED,
            _eph.STATE_LAUNCHED,
        )
        if state in active:
            return active.index(state)
        if state in _eph.TERMINAL_STATES:
            return len(active)
        raise ValueError(f"invalid launch request state {state!r}")

    def update_launch_request(self, request_id: str, updates: dict) -> dict | None:
        """Request-id checked, monotonic marker update."""
        with self._config_lock():
            marker = self.read_launch_request(request_id)
            if not marker:
                return None
            changes = dict(updates)
            if changes.get("request_id", request_id) != request_id:
                raise ValueError("launch request_id is immutable")
            current_state = marker.get("state", "queued")
            next_state = changes.get("state", current_state)
            current_rank = self._launch_state_rank(current_state)
            next_rank = self._launch_state_rank(next_state)
            if next_state != current_state and next_rank <= current_rank:
                raise ValueError(
                    f"cannot transition launch request {request_id!r} "
                    f"from {current_state!r} to {next_state!r}"
                )
            marker.update(changes)
            _atomic_write_text(self._launch_request_path(request_id),
                               json.dumps(marker, indent=2, ensure_ascii=False))
            return marker

    def archive_launch_request(self, request_id: str, archive_payload: dict) -> bool:
        """Archive and clear a launch-request marker ONLY if the current active
        marker has the same request_id. Returns True when archived."""
        with self._config_lock():
            marker = self.read_launch_request(request_id)
            if not marker:
                return False
            self.launch_requests_archive_dir.mkdir(parents=True, exist_ok=True)
            payload = dict(archive_payload)
            payload.setdefault("original", marker)
            payload.setdefault("request_id", request_id)
            dst = self.launch_requests_archive_dir / f"{request_id}.json"
            if dst.exists():
                dst = self.launch_requests_archive_dir / (
                    f"{request_id}.{_now_iso().replace(':', '-')}.json")
            _atomic_write_text(dst, json.dumps(payload, indent=2, ensure_ascii=False))
            try:
                self._launch_request_path(request_id).unlink()
            except OSError:
                return False
            return True

    # ------------------------------------------- dashboard intent queue (0.59.0)
    #
    # The web console can ONLY append a typed intent envelope here (architecture
    # C, docs/DASHBOARD-CONTROL-PLANE-DESIGN-HISTORY.md): `state/intents/active/<id>.json`
    # holds queued/claimed/recent-terminal intents (reset-CLEARED - a queued
    # intent references current-session state, so firing it into a fresh session
    # would be wrong), while `.agenttalk/control-audit/intents/` holds the
    # rotated TERMINAL audit records (reset-PRESERVED, like the dead-letter
    # sink). The EXECUTOR (`supervise --drain-intents`) is the authorization
    # boundary: it re-resolves the acting identity server-side and never trusts
    # origin/browser claims. write_intent is the only web-reachable mutation;
    # claim / attempt / terminal transitions are executor-only, under
    # `_config_lock` (the launch-request marker discipline).

    INTENT_QUEUED = "queued"
    INTENT_CLAIMED = "claimed"
    INTENT_APPLIED = "applied"
    INTENT_DENIED = "denied"
    INTENT_FAILED = "failed"
    INTENT_TERMINAL_STATES = frozenset({"applied", "denied", "failed"})
    # Persistent flood caps (roadmap: send/broadcast is the cheap-to-flood
    # surface). Refuse NEW intents (no write) once the active dir holds this
    # many files / bytes; the web layer maps the refusal to 429/507.
    INTENT_MAX_ACTIVE = 1000
    INTENT_MAX_ACTIVE_BYTES = 10 * 1024 * 1024
    # Terminal-audit retention (control-audit/intents): rotate_intents keeps at
    # most this window/size, oldest terminal records dropped first.
    INTENT_AUDIT_MAX_AGE_SECONDS = 7 * 24 * 3600.0
    INTENT_AUDIT_MAX_BYTES = 50 * 1024 * 1024

    @property
    def intents_active_dir(self) -> Path:
        return self.state_dir / "intents" / "active"

    @property
    def control_audit_dir(self) -> Path:
        """Top-level reset-PRESERVED control-plane audit sink."""
        return self.dir / "control-audit"

    def _intent_path(self, intent_id: str) -> Path:
        from agenttalk import ephemeral as _eph
        if not _eph.is_safe_id(intent_id):
            raise ValueError(f"unsafe intent id {intent_id!r}")
        return self.intents_active_dir / f"{intent_id}.json"

    class IntentCapacityError(RuntimeError):
        """Active intent dir is at its flood cap - nothing was written."""

        def __init__(self, message: str, *, code: str) -> None:
            super().__init__(message)
            self.code = code

    def _intent_active_usage(self) -> tuple[int, int]:
        d = self.intents_active_dir
        if not d.is_dir():
            return 0, 0
        n = size = 0
        for p in d.iterdir():
            if p.suffix != ".json":
                continue
            n += 1
            try:
                size += p.stat().st_size
            except OSError:
                continue
        return n, size

    def write_intent(self, kind: str, payload: dict, *,
                     origin: dict | None = None) -> dict:
        """Append ONE queued intent envelope (the only web-tier mutation).

        Validates the typed schema fail-closed (unknown kind / reserved or
        control keys REJECTED with ValueError - never silently stripped),
        enforces the active-dir flood caps (IntentCapacityError, no write),
        then atomically writes exactly one queued intent file under
        ``_config_lock``. ``origin`` is recorded as DIAGNOSTICS ONLY - it is
        an auditable assertion, never authority; the executor re-resolves."""
        from agenttalk import intents as _intents
        errors = _intents.validate_intent(kind, payload)
        if errors:
            raise ValueError("; ".join(errors))
        with self._config_lock():
            count, size = self._intent_active_usage()
            if count >= self.INTENT_MAX_ACTIVE:
                raise self.IntentCapacityError(
                    f"active intent cap reached ({count} >= "
                    f"{self.INTENT_MAX_ACTIVE}); drain or rotate first",
                    code="max_active")
            if size >= self.INTENT_MAX_ACTIVE_BYTES:
                raise self.IntentCapacityError(
                    "active intent byte cap reached; drain or rotate first",
                    code="max_bytes")
            intent_id = "wi-" + secrets.token_hex(6)
            record = {
                "schema_version": 1,
                "intent_id": intent_id,
                "kind": kind,
                "payload": dict(payload),
                "origin": dict(origin or {}),   # diagnostics only, NOT authority
                "created_at": _now_iso(),
                "state": self.INTENT_QUEUED,
                "attempts": 0,
                "deliveries": [],
            }
            self.intents_active_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(self._intent_path(intent_id),
                               json.dumps(record, indent=2, ensure_ascii=False))
            return record

    def read_intent(self, intent_id: str) -> dict | None:
        try:
            p = self._intent_path(intent_id)
        except ValueError:
            return None
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict) or data.get("intent_id") != intent_id:
            return None
        return data

    def _quarantine_active_intent_locked(self, path: Path, *, reason: str,
                                         quarantined_at: str,
                                         parse_error: str | None = None) -> dict | None:
        if path.suffix != ".json" or not path.exists():
            return None
        sink = self.control_audit_dir / "intents-invalid"
        sink.mkdir(parents=True, exist_ok=True)
        stamp = quarantined_at.replace(":", "-")
        dst: Path | None = None
        sidecar: Path | None = None
        for _ in range(100):
            candidate = sink / f"invalid-{stamp}-{secrets.token_hex(4)}.json"
            candidate_sidecar = sink / f"{candidate.name}.meta.json"
            if not candidate.exists() and not candidate_sidecar.exists():
                dst = candidate
                sidecar = candidate_sidecar
                break
        if dst is None or sidecar is None:
            return None
        try:
            shutil.move(str(path), str(dst))
        except OSError:
            return None
        meta = {
            "original_name": path.name,
            "quarantined_name": dst.name,
            "reason": reason,
            "quarantined_at": quarantined_at,
        }
        if parse_error:
            meta["parse_error"] = parse_error[:500]
        _atomic_write_text(sidecar, json.dumps(meta, indent=2, ensure_ascii=False))
        return {"from": str(path), "to": str(dst), **meta}

    def quarantine_invalid_intents(self, *, now_epoch: float | None = None,
                                   reason: str = "invalid_active_intent") -> dict:
        """Move unreadable/corrupt active intent JSON into reset-preserved audit.

        The scan is intentionally limited to ``state/intents/active/*.json`` and
        runs only under the executor path, never from dashboard GET/POST routes.
        """
        if now_epoch is not None:
            quarantined_at = datetime.fromtimestamp(
                now_epoch, timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z")
        else:
            quarantined_at = _now_iso()
        moved = 0
        with self._config_lock():
            d = self.intents_active_dir
            if not d.is_dir():
                return {"quarantined": 0}
            for p in sorted(d.iterdir()):
                if p.suffix != ".json":
                    continue
                parse_error: str | None = None
                invalid = False
                try:
                    self._intent_path(p.stem)
                except ValueError as e:
                    invalid = True
                    parse_error = f"{type(e).__name__}: {e}"
                try:
                    raw = p.read_bytes()
                    try:
                        data = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as e:
                        invalid = True
                        parse_error = f"{type(e).__name__}: {e}"
                    else:
                        if not isinstance(data, dict):
                            invalid = True
                            parse_error = "intent file root is not an object"
                        elif not invalid and data.get("intent_id") != p.stem:
                            invalid = True
                            parse_error = "intent_id does not match active filename"
                except OSError as e:
                    invalid = True
                    parse_error = f"{type(e).__name__}: {e}"
                if invalid and self._quarantine_active_intent_locked(
                    p, reason=reason, quarantined_at=quarantined_at,
                    parse_error=parse_error,
                ):
                    moved += 1
        return {"quarantined": moved}

    def list_intents(self, *, limit: int = 100,
                     include_terminal: bool = True) -> list[dict]:
        """Recent intents from the ACTIVE dir, newest first. Corrupt files are
        skipped (doctor-style fail-safe reading)."""
        d = self.intents_active_dir
        if not d.is_dir():
            return []
        out: list[dict] = []
        for p in sorted(d.iterdir()):
            if p.suffix != ".json":
                continue
            rec = self.read_intent(p.stem)
            if rec is None:
                continue
            if not include_terminal and rec.get("state") in self.INTENT_TERMINAL_STATES:
                continue
            out.append(rec)
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return out[: max(0, int(limit))]

    def _write_intent_locked(self, record: dict) -> None:
        _atomic_write_text(self._intent_path(record["intent_id"]),
                           json.dumps(record, indent=2, ensure_ascii=False))

    def claim_intent(self, intent_id: str, *, pid: int,
                     pid_start: object = None,
                     claim_stale_after: float = 900.0,
                     now_epoch: float | None = None) -> dict | None:
        """Atomically claim a queued intent (executor-only).

        A CLAIMED intent is reclaimable ONLY when its recorded owner pid is
        CONFIRMED dead (:func:`_process_liveness` == PROC_DEAD) - live, unknown,
        access-denied, or unprobeable owners are NEVER stolen (the D-12
        confirmed-dead discipline), so a descheduled-but-live drainer blocks a
        reclaim instead of racing it into a double-send. ``claim_stale_after``
        only bounds how soon we even probe a claim's owner. Returns the updated
        record, or None (absent / terminal / validly claimed)."""
        now = now_epoch if now_epoch is not None else time.time()
        with self._config_lock():
            rec = self.read_intent(intent_id)
            if not rec:
                return None
            state = rec.get("state")
            if state in self.INTENT_TERMINAL_STATES:
                return None
            if state == self.INTENT_CLAIMED:
                claim = rec.get("claim") if isinstance(rec.get("claim"), dict) else {}
                owner = claim.get("pid")
                age = now - float(claim.get("at_epoch") or 0.0)
                if owner == pid:
                    if not _same_pid_claim_allowed(
                        owner, claim.get("pid_start"), pid_start,
                    ):
                        return None
                elif age < claim_stale_after:
                    return None                # fresh claim: never contested
                elif not _owner_identity_gone(owner, claim.get("pid_start")):
                    return None                # live/unknown owner: NEVER stolen
            rec["state"] = self.INTENT_CLAIMED
            rec["attempts"] = int(rec.get("attempts") or 0) + 1
            rec["claim"] = {"pid": pid, "pid_start": pid_start,
                            "claim_id": secrets.token_hex(8),
                            "at": _now_iso(), "at_epoch": now}
            self._write_intent_locked(rec)
            return rec

    def update_intent(self, intent_id: str, mutate) -> dict | None:
        """Executor-only locked read-modify-write: ``mutate(record)`` edits in
        place; the result is atomically persisted. None = absent/corrupt."""
        with self._config_lock():
            rec = self.read_intent(intent_id)
            if not rec:
                return None
            mutate(rec)
            self._write_intent_locked(rec)
            return rec

    def mark_intent_terminal(self, intent_id: str, *, state: str,
                             code: str | None = None,
                             error: str | None = None) -> dict | None:
        if state not in self.INTENT_TERMINAL_STATES:
            raise ValueError(f"not a terminal intent state: {state!r}")

        def _mut(rec: dict) -> None:
            rec["state"] = state
            rec["terminal_at"] = _now_iso()
            if code:
                rec["code"] = code
            if error:
                rec["error"] = error

        return self.update_intent(intent_id, _mut)

    def rotate_intents(self, *, now_epoch: float | None = None,
                       terminal_linger_seconds: float = 600.0) -> dict:
        """Move settled TERMINAL intents from the active dir into the
        reset-preserved control-audit sink, then enforce the audit retention
        caps (age + bytes, oldest first). Best-effort; returns counts."""
        from agenttalk.health import parse_iso as _parse_iso_dt
        now = now_epoch if now_epoch is not None else time.time()
        moved = dropped = quarantined_invalid = 0
        quarantined_at = datetime.fromtimestamp(
            now, timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z")
        with self._config_lock():
            d = self.intents_active_dir
            if d.is_dir():
                audit = self.control_audit_dir / "intents"
                for p in sorted(d.iterdir()):
                    if p.suffix != ".json":
                        continue
                    rec = self.read_intent(p.stem)
                    if not rec or rec.get("state") not in self.INTENT_TERMINAL_STATES:
                        continue
                    ts = _parse_iso_dt(rec.get("terminal_at") or rec.get("created_at"))
                    age = (now - ts.timestamp()) if ts is not None else None
                    if age is None or age < terminal_linger_seconds:
                        if age is None and self._quarantine_active_intent_locked(
                            p, reason="invalid_intent_timestamp",
                            quarantined_at=quarantined_at,
                            parse_error="terminal_at/created_at is unparseable",
                        ):
                            quarantined_invalid += 1
                        continue
                    audit.mkdir(parents=True, exist_ok=True)
                    _atomic_write_text(audit / p.name,
                                       json.dumps(rec, indent=2, ensure_ascii=False))
                    try:
                        p.unlink()
                        moved += 1
                    except OSError:
                        continue
        # Retention on the audit sink (outside the config lock: audit-only).
        audit = self.control_audit_dir / "intents"
        if audit.is_dir():
            entries = []
            total = 0
            for p in sorted(audit.iterdir()):
                if p.suffix != ".json":
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                entries.append((p, st.st_mtime, st.st_size, p.name))
                total += st.st_size
            entries.sort(key=lambda item: (item[1], item[3]))
            for p, mtime, size, _name in entries:
                too_old = (now - mtime) > self.INTENT_AUDIT_MAX_AGE_SECONDS
                if too_old or total > self.INTENT_AUDIT_MAX_BYTES:
                    try:
                        p.unlink()
                        dropped += 1
                        total -= size
                    except OSError:
                        pass
        return {"rotated": moved, "audit_dropped": dropped,
                "quarantined_invalid": quarantined_invalid}

    # ---------------------------------- supervisor kill-switch + instance lock

    def supervisor_kill_switch(self) -> bool | None:
        """Tri-state kill-switch read for the web fast-fail: True = present,
        False = absent, None = UNREADABLE (callers fail closed, e.g. 423)."""
        try:
            return (self.dir / "supervisor.kill").exists()
        except OSError:
            return None

    def supervisor_instance_path(self) -> Path:
        return self.dir / "supervisor.instance.lock"

    def _read_supervisor_instance_strict_locked(
        self,
    ) -> tuple[str, dict | None, str | None]:
        """Read the singleton marker without conflating corruption with absence.

        The caller owns the lifecycle lock.  Status is ``absent``, ``valid``, or
        ``invalid``.  A marker is valid only when all fields needed for a
        token/pid/start checked release are well-formed and rooted here.
        """
        path = self.supervisor_instance_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "absent", None, None
        except OSError as exc:
            return "invalid", None, f"unreadable marker: {exc}"
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return "invalid", None, f"malformed marker JSON: {exc}"
        if not isinstance(data, dict):
            return "invalid", None, "marker is not a JSON object"
        if data.get("root") != str(self.root):
            return "invalid", None, "marker root does not match this project"
        pid = data.get("pid")
        token = data.get("token")
        started_at = data.get("started_at")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "invalid", None, "marker pid is invalid"
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            return "invalid", None, "marker token is invalid"
        if not isinstance(started_at, str) or _parse_start_token(started_at) is None:
            return "invalid", None, "marker started_at is invalid"
        pid_start = data.get("pid_start")
        if pid_start is not None and (not isinstance(pid_start, str) or not pid_start):
            return "invalid", None, "marker pid_start is invalid"
        return "valid", data, None

    def read_supervisor_instance_strict(self) -> tuple[str, dict | None, str | None]:
        """Strict singleton read for diagnostics; mutation paths read under lock."""
        return self._read_supervisor_instance_strict_locked()

    def read_supervisor_instance(self) -> dict | None:
        status, data, _ = self._read_supervisor_instance_strict_locked()
        return data if status == "valid" else None

    def _quarantine_supervisor_instance_locked(self, *, reason: str) -> Path:
        """Move the current marker aside while the lifecycle lock is held."""
        source = self.supervisor_instance_path()
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = source.with_name(f"{source.name}.quarantine-{suffix}")
        os.replace(source, target)
        audit = self.dir / "supervisor-instance-repairs.jsonl"
        with audit.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({
                "at": _now_iso(),
                "action": "quarantine",
                "reason": reason,
                "source": str(source),
                "target": str(target),
            }, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return target

    def quarantine_invalid_supervisor_instance(self, *, reason: str) -> Path | None:
        """Explicit operator recovery for an invalid/unreadable marker only."""
        with self._supervisor_lifecycle_lock():
            status, _data, detail = self._read_supervisor_instance_strict_locked()
            if status == "absent":
                return None
            if status == "valid":
                raise ValueError("refusing to quarantine a structurally valid supervisor marker")
            return self._quarantine_supervisor_instance_locked(
                reason=f"{reason}: {detail or 'invalid marker'}"
            )

    def _claim_supervisor_instance_locked(self, *, pid: int,
                                          pid_start: object = None) -> dict | None:
        """Generic marker fold/write; caller holds lifecycle and config locks."""
        status, existing, _detail = self._read_supervisor_instance_strict_locked()
        if status == "invalid":
            return None
        if existing is not None:
            existing_pid = existing.get("pid")
            if existing_pid == pid:
                reclaim = _same_pid_claim_allowed(
                    existing_pid, existing.get("pid_start"), pid_start,
                )
            else:
                reclaim = _owner_identity_gone(
                    existing_pid, existing.get("pid_start"),
                )
            if not reclaim:
                return None
            self._quarantine_supervisor_instance_locked(reason="confirmed stale or pid-reused owner")
        record = {
            "root": str(self.root),
            "pid": int(pid),
            "pid_start": pid_start,
            "token": secrets.token_hex(16),
            "started_at": _now_iso(),
        }
        _atomic_write_text(
            self.supervisor_instance_path(),
            json.dumps(record, indent=2, ensure_ascii=False),
        )
        return record

    def claim_supervisor_instance(self, *, pid: int,
                                  pid_start: object = None) -> dict | None:
        """Claim the SINGLETON supervisor/executor instance lock.

        Held for the supervisor process lifetime (released in the .ps1
        ``finally``). A held lock is broken ONLY when its recorded pid is
        CONFIRMED dead (never on age - a process-lifetime lock has no honest
        age bound; the confirmed-dead tri-state is the D-12 discipline). The
        returned record carries the random ``token`` the PS loop passes back
        to every ``--drain-intents`` tick. ANTI-ACCIDENT, not a security
        boundary: the token rides the command line and the pid is the
        caller's own claim (documented in SECURITY.md)."""
        with self._supervisor_lifecycle_lock():
            with self._config_lock():
                return self._claim_supervisor_instance_locked(
                    pid=pid, pid_start=pid_start,
                )

    def release_supervisor_instance(self, *, token: str, pid: int | None = None,
                                    pid_start: object = None) -> bool:
        """Token/pid-checked release. A mismatched/absent token releases nothing
        (a stale releaser can never evict a newer live instance)."""
        with self._supervisor_lifecycle_lock():
            with self._config_lock():
                status, existing, _detail = self._read_supervisor_instance_strict_locked()
                if status != "valid" or not existing:
                    return False
                if not token or existing.get("token") != token:
                    return False
                if pid is not None and existing.get("pid") != int(pid):
                    return False
                if pid_start is not None and existing.get("pid_start") != pid_start:
                    return False
                try:
                    self.supervisor_instance_path().unlink()
                except OSError:
                    return False
                return True

    # ------------------------------------------- reply-in-flight markers
    #
    # `state/<agent>.composing.json` records "agent is drafting a reply
    # on thread <rid>" — written by `composing --to-request`, read by
    # threads/sync display so a counterparty sees a reply in flight and
    # does not fire a crossing message. STRICTLY observational, same
    # discipline as `.heartbeat`/`.waiting`: nothing about delivery,
    # cursors, or thread closure depends on it; missing/corrupt reads as
    # "no marker". Staleness is the READER's job: an entry older than
    # COMPOSING_INTENT_STALE_SECONDS is ignored. Added 0.14.0 (#14).

    def write_composing_intent(self, agent: str, request_id: str, peer: str) -> None:
        """Best-effort upsert of the reply-in-flight record for one thread."""
        p = self.state_dir / f"{agent}.composing.json"
        data = self.read_composing_intent(agent)
        threads = data.get("threads")
        if not isinstance(threads, dict):
            threads = {}
        threads[request_id] = {"peer": peer, "at": _now_iso()}
        try:
            _atomic_write_text(
                p, json.dumps({"agent": agent, "threads": threads}, ensure_ascii=False)
            )
        except OSError:
            pass  # observability only — a failed write degrades to "no marker"

    def read_composing_intent(self, agent: str) -> dict:
        """Return the parsed marker ({} if absent/corrupt). Never raises.

        Shape: ``{"agent": <name>, "threads": {<rid>: {"peer": ..., "at": ISO}}}``.
        Callers read ``.get("threads", {})`` and apply the staleness rule.
        """
        p = self.state_dir / f"{agent}.composing.json"
        if not p.exists():
            return {}
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def clear_composing_intent(self, agent: str, request_id: str | None = None) -> None:
        """Drop one thread's entry (or the whole marker). Best-effort."""
        p = self.state_dir / f"{agent}.composing.json"
        if request_id is None:
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass
            return
        data = self.read_composing_intent(agent)
        threads = data.get("threads")
        if not isinstance(threads, dict) or request_id not in threads:
            return
        threads.pop(request_id, None)
        try:
            if threads:
                _atomic_write_text(
                    p, json.dumps({"agent": agent, "threads": threads}, ensure_ascii=False)
                )
            else:
                p.unlink()
        except (FileNotFoundError, OSError):
            pass

    # --------------------------------------------------- capacity (budget)
    #
    # Advisory rate-limit budget snapshots an agent self-publishes so a lead
    # can factor remaining 5h/weekly budget into how it organizes work. Like
    # the heartbeat/composing markers, this is STRICTLY observational: a
    # missing/corrupt/stale snapshot never blocks protocol progress. The
    # snapshot carries only derived budget metadata (see capacity.py), never
    # account ids, auth paths, or token/session contents.

    def write_capacity(self, agent: str, snapshot: dict) -> None:
        """Best-effort publish of ``agent``'s budget snapshot to the bus."""
        p = self.state_dir / f"{agent}.capacity.json"
        try:
            _atomic_write_text(p, json.dumps(snapshot, ensure_ascii=False))
        except (OSError, TypeError):
            pass  # observability only — a failed write degrades to "no snapshot"

    def read_capacity(self, agent: str) -> dict | None:
        """Return ``agent``'s published snapshot dict, or None if
        absent/empty/corrupt. Never raises."""
        p = self.state_dir / f"{agent}.capacity.json"
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def read_all_capacities(self) -> dict[str, dict]:
        """All published snapshots keyed by agent (derived from the state dir,
        so a retired/forgotten agent's stale file still surfaces). Skips
        absent/corrupt files."""
        out: dict[str, dict] = {}
        if not self.state_dir.is_dir():
            return out
        suffix = ".capacity.json"
        for p in sorted(self.state_dir.glob(f"*{suffix}")):
            agent = p.name[: -len(suffix)]
            d = self.read_capacity(agent)
            if d is not None:
                out[agent] = d
        return out

    # ----------------------------------------- wrapper runtime (v0.75.0)
    #
    # A fail-safe ALLOW-LIST PROJECTION of a wrapped agent's session state for the
    # dashboard. The raw <agent>.wrapper-session.json holds durable session
    # identities (codex_thread_id / claude_session_id); this reader DROPS them at
    # the boundary so they can NEVER reach web.py / the wire. reset_reason is mapped
    # through a CLOSED space-free token set (free text is never leaked).

    _RESET_REASON_TOKENS = frozenset({
        "runtime_config_changed",   # v0.75.0 model/effort drift forced a fresh session
        "resume_unavailable",       # codex/claude resume gave up (2 session failures)
        "session_full",             # a session-full reset (forward-compat)
    })

    @classmethod
    def _map_reset_reason(cls, raw: object) -> str | None:
        """Map a (possibly multi-word) ``continuity_lost_reason`` to a CLOSED
        space-free token, or None. Takes the first whitespace-delimited token and
        returns it ONLY if it is in the closed set — so free text is never leaked.
        'resume_unavailable after 2 session failures' -> 'resume_unavailable';
        'runtime_config_changed' -> itself; 'stream disconnected' -> None."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        token = raw.strip().split()[0]
        return token if token in cls._RESET_REASON_TOKENS else None

    def read_wrapper_runtime(self, agent: str) -> dict | None:
        """Fail-safe allow-list projection of ``agent``'s wrapper-session state:
        ``{model?, reasoning_effort?, session_state, reset_reason?, turns}``. The
        durable session ids are DROPPED here (never returned). Missing / empty /
        corrupt / non-dict -> None; never raises."""
        p = self.state_dir / f"{agent}.wrapper-session.json"
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        out: dict = {}
        model = d.get("model")
        if isinstance(model, str) and model.strip():
            out["model"] = model
        effort = d.get("reasoning_effort")
        if isinstance(effort, str) and effort.strip():
            out["reasoning_effort"] = effort
        turns = d.get("turns")
        turns = turns if isinstance(turns, int) and not isinstance(turns, bool) else 0
        out["turns"] = turns
        thread_id = d.get("codex_thread_id")
        session_id = d.get("claude_session_id")
        has_id = bool(
            (isinstance(thread_id, str) and thread_id)
            or (isinstance(session_id, str) and session_id))
        resumed = bool(d.get("resume_available")) and has_id and turns > 0
        out["session_state"] = "resumed" if resumed else "fresh"
        reason = self._map_reset_reason(d.get("continuity_lost_reason"))
        if reason is not None:
            out["reset_reason"] = reason
        return out

    # ------------------------------------------------------- thread state
    #
    # Per-(agent, request_id) state for SCOPED thread work, kept separate
    # from the single global per-agent cursor. Two distinct notions:
    #   seen_msg_id — the newest message on this thread a SCOPED wait has
    #                 returned to the agent. Lets `wait --to-request` make
    #                 progress (don't re-return the same message) WITHOUT
    #                 consuming the global cursor, so unrelated inbox
    #                 traffic stays unread for a later `drain`. "Seen by a
    #                 scoped wait" is NOT "handled".
    #   closed      — the agent has explicitly closed the thread (manual
    #                 `ack --to-request`). ONLY this clears an owed/
    #                 actionable thread in `threads`/`sync`; seen_msg_id
    #                 alone never does — so a restart after a scoped wait
    #                 displayed a message but before the agent acted still
    #                 surfaces the thread as actionable. (0.12.0)

    def read_threadstate(self, agent: str) -> dict:
        """Return ``{request_id: {seen_msg_id, closed, ...}}`` for ``agent``.

        Never raises — a missing/corrupt/partially-written file reads as
        ``{}`` (degrade to "no scoped state", same as a fresh agent).
        """
        p = self.state_dir / f"{agent}.threadstate.json"
        if not p.exists():
            return {}
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_threadstate(self, agent: str, data: dict) -> None:
        p = self.state_dir / f"{agent}.threadstate.json"
        _atomic_write_text(p, json.dumps(data, indent=2, ensure_ascii=False))

    def thread_seen(self, agent: str, request_id: str) -> str:
        """The newest msg id a scoped wait has returned to ``agent`` on this
        thread (``""`` if none)."""
        entry = self.read_threadstate(agent).get(request_id)
        if isinstance(entry, dict):
            sid = entry.get("seen_msg_id")
            return sid if isinstance(sid, str) else ""
        return ""

    def mark_thread_seen(self, agent: str, request_id: str, msg_id: str) -> None:
        """Advance ``seen_msg_id`` (monotonic) — used by `wait --to-request`.
        Does NOT set ``closed``: seeing a message is not handling it."""
        data = self.read_threadstate(agent)
        entry = data.get(request_id)
        if not isinstance(entry, dict):
            entry = {}
        cur = entry.get("seen_msg_id")
        if not isinstance(cur, str) or msg_id > cur:
            entry["seen_msg_id"] = msg_id
            entry.setdefault("closed", False)
            data[request_id] = entry
            self._write_threadstate(agent, data)

    def close_thread(self, agent: str, request_id: str, *,
                     seen_msg_id: str | None = None,
                     reason: str = "manual") -> None:
        """Explicitly close a thread for ``agent`` (`ack --to-request`).

        Sets ``closed=true`` — the only thing that clears an owed/
        actionable thread in derivation — and advances ``seen_msg_id`` to
        ``seen_msg_id`` (the latest matching id at ack time) if newer.
        """
        data = self.read_threadstate(agent)
        entry = data.get(request_id)
        if not isinstance(entry, dict):
            entry = {}
        if seen_msg_id is not None:
            cur = entry.get("seen_msg_id")
            if not isinstance(cur, str) or seen_msg_id > cur:
                entry["seen_msg_id"] = seen_msg_id
        entry["closed"] = True
        entry["closed_at"] = _now_iso()
        entry["closed_reason"] = reason
        data[request_id] = entry
        self._write_threadstate(agent, data)
        try:
            from agenttalk.wrapper.obligations import note_manual_close

            note_manual_close(self, agent, request_id)
        except (OSError, ValueError, RuntimeError):
            pass

    def thread_closed(self, agent: str, request_id: str) -> bool:
        """True iff ``agent`` has explicitly closed this thread.

        Strict identity (``is True``) so a malformed non-boolean ``closed``
        value in a hand-edited threadstate can't accidentally close a
        thread."""
        entry = self.read_threadstate(agent).get(request_id)
        return isinstance(entry, dict) and entry.get("closed") is True


# --------------------------------------------------------- helpers (module)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


_id_lock = threading.Lock()
_last_id_dt: datetime | None = None

# Default freshness window for `foreign_wait_pid` when the caller does not
# pass one. Generous on purpose: the liveness check is the real gate, this
# only discards an obviously-expired bounded-wait marker.
_WAIT_STALE_AFTER_DEFAULT = 300.0

# An identity counts as ACTIVE (someone is using this name) when its heartbeat
# is fresher than this OR it has a live waiting marker. Used by the unique-name
# self-join guard (`roster add --unique`) to refuse re-binding a live identity.
ACTIVE_WITHIN_SECONDS = 120.0

# Managed lead-loop (lead-loop Slice 1): a wrapped controller that OWNS a team
# mailbox via a renewable lease. The lease is the correctness state; the .waiting
# marker only MIRRORS it for status/UX. The TTL must EXCEED the renew cadence so a
# single missed renewal (a long turn) never expires a healthy owner; only a
# sustained gap + a stale heartbeat / dead owner makes the lease stealable.
LEAD_LOOP_MODE = "lead-loop"
LEAD_LOOP_CADENCE_DEFAULT = 300.0
LEAD_LOOP_TTL_DEFAULT = 900.0
# The owner-bypass env token for the single-consumer guard (cli._guard_lead_loop_consumer
# reads it; the wrapped controller presents it for its OWN in-process consumption). SINGLE
# source so the wrapper can STRIP it from the model child's environment (WP2) without
# duplicating the literal. It is advisory coordination inside the trusted state dir, NOT
# authz (D-4): never log it, never expose it via a read-only command, never pass it to the
# model child.
LEAD_LOOP_LEASE_ENV = "AGENTTALK_LEAD_LOOP_LEASE"

# Managed lead-loop CADENCE TICK (lead-loop Slice 2 WP3): the proactive sweep the
# controller drives when the bus is QUIET and the cadence interval has elapsed - a
# SYNTHETIC, wrapper-owned turn that never consumes a bus record (no cursor advance,
# no attempt ledger, no dead-letter path). The per-agent cadence STATE
# (state/<agent>.lead-loop-cadence.json) is controller-owned single-writer and is
# reset-cleared like the lease (reset() deletes state_dir wholesale; the dead-letter
# SINK lives elsewhere and is preserved).
LEAD_LOOP_REMINDER_AFTER_DEFAULT = 1800.0     # open-outbound reminder window (s)
LEAD_LOOP_CADENCE_FAIL_BACKOFF_BASE = 60.0    # first failed-tick backoff (s)
LEAD_LOOP_CADENCE_FAIL_BACKOFF_MAX = 1800.0   # backoff ceiling (s)
LEAD_LOOP_CADENCE_HEALTH_THRESHOLD = 5        # consecutive failed ticks -> escalate
#                                               controller-HEALTH (NOT message poison)


def _process_alive(pid: int) -> bool:
    """Best-effort, stdlib, fail-quiet liveness check (0.18.0, FR-007).

    Returns True only when ``pid`` is positive, an int, and currently
    running. NEVER raises: an uncertain probe returns False so the
    duplicate-activation warning errs toward silence rather than a false
    alarm or a crash.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes  # stdlib; imported lazily so POSIX never pays for it
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            # Declare prototypes: a Win32 HANDLE is pointer-sized, but
            # ctypes defaults restype/argtypes to c_int (32-bit), which
            # truncates/sign-extends the handle on 64-bit Windows and would
            # query/close the wrong handle. Set them explicitly.
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                             wintypes.DWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 — fail-quiet to "not alive"
            return False
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False


# Tri-state liveness for AUTHORITY decisions (lead-loop lease steal / armed / guard).
PROC_ALIVE = "alive"
PROC_DEAD = "dead"
PROC_UNKNOWN = "unknown"
# Anti-reuse compares should be tight: widening this risks false-matching a
# recycled pid. Ambiguous tokens already degrade to None and are not stealable.
_START_TOKEN_COMPARE_TOLERANCE_SECONDS = 0.001


def _process_liveness(pid: object) -> str:
    """Tri-state liveness probe for the lead-loop AUTHORITY decisions.

    Returns one of:
      ``PROC_ALIVE``   - the pid is CONFIRMED running.
      ``PROC_DEAD``    - a DEFINITIVE not-running signal. This is the ONLY state
                         that authorizes an immediate lease steal, so it must never
                         be a guess: POSIX ``os.kill(pid,0)`` raising
                         ``ProcessLookupError`` (ESRCH); Windows
                         ``GetExitCodeProcess`` returning an exit code other than
                         ``STILL_ACTIVE`` (the process has exited), or
                         ``OpenProcess`` failing with ``ERROR_INVALID_PARAMETER``
                         (no such pid).
      ``PROC_UNKNOWN`` - the probe was uncertain: access-denied, any ambiguous
                         OpenProcess failure, a non-positive/non-int pid, or any
                         raised exception. Callers MUST treat UNKNOWN as
                         probably-alive and fall back to the expired-AND-heartbeat-
                         stale recovery path - NEVER steal on it.

    This is deliberately STRONGER than the fail-quiet :func:`_process_alive`, which
    collapses unknown into not-alive (False). The lead-loop steal/armed/guard use
    this so an immediate dead-owner steal can NEVER displace a live controller whose
    probe merely failed (reviewer-1/codex blocker on the immediate-steal change;
    lead D-12 ruling = Option A). UNKNOWN errs safe in every direction: probably-
    alive => armed, guarded, and not stolen until the lease both expires AND its
    heartbeat goes stale. A non-positive/non-int pid is UNKNOWN (not DEAD): only the
    enumerated OS signals are definitive enough to authorize a steal."""
    if not isinstance(pid, int) or pid <= 0:
        return PROC_UNKNOWN
    if os.name == "nt":
        try:
            import ctypes  # stdlib; lazy so POSIX never pays for it
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_INVALID_PARAMETER = 87
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                             wintypes.DWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                err = ctypes.get_last_error()
                # No such pid = confirmed dead. Anything else (ACCESS_DENIED, etc.)
                # means the process may exist -> UNKNOWN, never steal.
                return PROC_DEAD if err == ERROR_INVALID_PARAMETER else PROC_UNKNOWN
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return PROC_UNKNOWN
                return PROC_ALIVE if code.value == STILL_ACTIVE else PROC_DEAD
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - uncertain probe -> UNKNOWN, never steal
            return PROC_UNKNOWN
    # POSIX
    try:
        os.kill(pid, 0)
        return PROC_ALIVE
    except ProcessLookupError:
        return PROC_DEAD
    except PermissionError:
        return PROC_ALIVE  # exists, owned by another user
    except OSError:
        return PROC_UNKNOWN  # uncertain -> never steal


def _process_start_token(pid: object) -> str | None:
    """Best-effort process start token, or None on any ambiguity.

    A non-None token is only returned when the OS source is specific enough to
    compare against a previously recorded start. Callers must treat None as
    conservative/possibly-same-process.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes  # stdlib; lazy so POSIX never pays for it
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            FILETIME_EPOCH_DELTA_SECONDS = 11644473600
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                             wintypes.DWORD]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel_time), ctypes.byref(user_time))
                if not ok:
                    return None
                ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
                if ticks <= 0:
                    return None
                seconds = (ticks / 10_000_000.0) - FILETIME_EPOCH_DELTA_SECONDS
                return datetime.fromtimestamp(seconds, timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z")
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - ambiguity -> unknown token
            return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        rparen = stat.rfind(")")
        if rparen < 0:
            return None
        fields = stat[rparen + 2:].split()
        if len(fields) < 20:
            return None
        start_ticks = fields[19]
        if not start_ticks.isdigit():
            return None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{32,64}", boot_id):
            return None
        return f"linux:{boot_id}:{start_ticks}"
    except Exception:  # noqa: BLE001 - missing /proc, access denied, races -> None
        return None


def _parse_start_token(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    match = re.fullmatch(
        r"(.+T\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})",
        text,
    )
    if match:
        prefix, frac, zone = match.groups()
        if frac:
            frac = "." + frac[1:7].ljust(6, "0")
        else:
            frac = ""
        text = prefix + frac + ("+00:00" if zone == "Z" else zone)
    else:
        text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _start_tokens_differ(observed: object, recorded: object) -> bool:
    if not isinstance(observed, str) or not observed:
        return False
    if not isinstance(recorded, str) or not recorded:
        return False
    if observed.startswith("linux:") or recorded.startswith("linux:"):
        return observed.startswith("linux:") and recorded.startswith("linux:") and observed != recorded
    obs_dt = _parse_start_token(observed)
    rec_dt = _parse_start_token(recorded)
    if obs_dt is None or rec_dt is None:
        return False
    return (
        abs(obs_dt.timestamp() - rec_dt.timestamp())
        > _START_TOKEN_COMPARE_TOLERANCE_SECONDS
    )


def _start_tokens_same(left: object, right: object) -> bool:
    if not isinstance(left, str) or not left:
        return False
    if not isinstance(right, str) or not right:
        return False
    if left.startswith("linux:") or right.startswith("linux:"):
        return left.startswith("linux:") and right.startswith("linux:") and left == right
    left_dt = _parse_start_token(left)
    right_dt = _parse_start_token(right)
    if left_dt is None or right_dt is None:
        return False
    return (
        abs(left_dt.timestamp() - right_dt.timestamp())
        <= _START_TOKEN_COMPARE_TOLERANCE_SECONDS
    )


def _owner_identity_gone(pid: object, recorded_pid_start: object) -> bool:
    """True only when the recorded owner is confidently not the same process."""
    liveness = _process_liveness(pid)
    if liveness == PROC_DEAD:
        return True
    if liveness != PROC_ALIVE:
        return False
    return _start_tokens_differ(_process_start_token(pid), recorded_pid_start)


def _same_pid_claim_allowed(pid: object, recorded_pid_start: object,
                            caller_pid_start: object) -> bool:
    """Whether a caller using the same numeric pid may refresh/reclaim a claim."""
    if _start_tokens_same(caller_pid_start, recorded_pid_start):
        return True
    observed = _process_start_token(pid)
    return (
        _start_tokens_differ(observed, recorded_pid_start)
        and _start_tokens_same(observed, caller_pid_start)
    )


def _new_id() -> str:
    """Return a fresh message id, monotonic within this process.

    Format: ``YYYYMMDD-HHMMSS-uuuuuu-XXXX``. The timestamp is forced
    strictly greater than the previous id issued by this process, so
    lexicographic order matches send order for any single writer —
    the invariant the bus and dashboard rely on for chronology
    (``messages_for`` sorts by id; on fast hardware two ``send()``
    calls can land in the same microsecond, and the random suffix
    alone does not preserve order).

    Cross-process collisions (two agents writing the same
    microsecond) are still handled by the 4-char random suffix —
    each process tracks its own ``_last_id_dt``.
    """
    global _last_id_dt
    with _id_lock:
        now = datetime.now(timezone.utc)
        if _last_id_dt is not None and now <= _last_id_dt:
            now = _last_id_dt + timedelta(microseconds=1)
        _last_id_dt = now
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
    return now.strftime("%Y%m%d-%H%M%S-%f") + "-" + suffix


def _new_session_id() -> str:
    """Return a unique session identifier. Includes a random suffix
    so two calls in the same second (e.g. init then reset) get
    distinct IDs — otherwise `archived/<session_id>/` collides.
    """
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
    return f"{base}-{suffix}Z"


def find_root(start: Path | None = None) -> Path:
    """Resolve the bus root. Precedence: --root flag > AGENTTALK_ROOT > upward walk.

    The explicit ``--root`` flag is handled by callers (they bypass this
    function entirely), so here the order is: a non-empty
    ``AGENTTALK_ROOT`` environment variable wins — and is returned
    **whether or not a store exists there**, so the caller's must-exist
    check fails loudly exactly like an invalid ``--root`` (the env var
    never silently falls back to the walk; a typo'd pin must not route
    a window to a different store). Otherwise: walk upward from
    ``start`` (or CWD) to the first ancestor containing ``.agenttalk/``,
    falling back to the start dir so ``init`` can create a fresh store.
    AGENTTALK_ROOT is read HERE and nowhere else. Added 0.14.0 (#13).
    """
    env = os.environ.get("AGENTTALK_ROOT")
    if env:
        return Path(env).resolve()
    start = Path(start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        if (d / DIRNAME).is_dir():
            return d
    return start


def find_stores_upward(start: Path | None = None) -> list[Path]:
    """Every ancestor (start inclusive → filesystem root) containing a
    ``.agenttalk/`` store, in walk order.

    The split-brain mechanism behind the production "--root gotcha" is
    two ``init``s at different depths: both stores are valid, neither
    errors, and two windows resolve to different roots. This scanner
    powers the loud diagnostics: ``init``'s up-tree refusal and
    ``doctor``'s multi-store report. Added 0.14.0 (#13).
    """
    start = Path(start or Path.cwd()).resolve()
    return [d for d in [start, *start.parents] if (d / DIRNAME).is_dir()]


def validate_rescind(
    store: Store,
    sender: str,
    request_id: str,
    target_msg_id: str | None = None,
) -> list[Message]:
    """Validate a rescind attempt; return the thread's opener copies.

    Rules (research.md D2): only the thread's **requester** — the sender
    of its opener(s) — may rescind it, and the thread must be visible in
    ``valid_messages()`` (visibility matches derivation, so you cannot
    rescind what derivation cannot see). ``target_msg_id``, when given,
    must be a message in the thread.

    Returns the opener copies in id order: one for a pairwise thread,
    one per recipient for a broadcast fan-out (all sharing the same
    sender). The caller addresses one rescind message to each distinct
    opener recipient. Raises ``ValueError`` with an actionable message
    otherwise.
    """
    msgs = store.valid_messages()
    thread = [m for m in msgs if (m.meta or {}).get("request_id") == request_id]
    if not thread:
        raise ValueError(
            f"no thread with request_id {request_id!r} is visible — check the id "
            f"(agenttalk threads --for {sender}) and that you are on the right --root"
        )
    openers = [m for m in thread if m.kind in OPENER_KINDS]
    if not openers:
        raise ValueError(
            f"thread {request_id!r} has no visible opener (review-request/"
            f"question/proposal) — nothing to rescind"
        )
    requester = openers[0].sender  # fan-out copies share one sender
    if sender != requester:
        raise ValueError(
            f"only the requester ({requester!r}) may rescind thread "
            f"{request_id!r}; {sender!r} did not open it"
        )
    if target_msg_id is not None and not any(m.id == target_msg_id for m in thread):
        raise ValueError(
            f"--to-id {target_msg_id!r} is not a message in thread {request_id!r}"
        )
    return openers
