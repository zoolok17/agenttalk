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
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import string
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk import signing as _signing

DIRNAME = ".agenttalk"
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
        if roster:
            if self.sender not in roster:
                raise ValueError(
                    f"sender {self.sender!r} not in roster {sorted(roster)}"
                )
            if self.recipient not in roster:
                raise ValueError(
                    f"recipient {self.recipient!r} not in roster {sorted(roster)}"
                )


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
            # NOTE: no project_id in config.json. The HMAC key file
            # is addressed by `signing.project_id_for_root(self.root)`,
            # a path-derived hash that an attacker writing into
            # .agenttalk/ cannot influence. See SECURITY.md.
        }
        if retired_carry:
            cfg["retired"] = retired_carry  # tombstones survive a force re-init
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
        - **deletes** ``messages/`` and ``state/`` (active bus state)
        - **preserves** ``sessions/`` (historical transcript exports
          — those are user-visible artifacts, not active bus state)
        - bumps ``session_id``

        With ``archive=True``:
        - **moves** ``messages/``, ``state/``, AND ``sessions/`` into
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
                old_session_id, subdirs=("messages", "state", "sessions"),
            )
        else:
            # Default delete: messages + state only. sessions/ holds
            # exported transcripts (a user-visible artifact) — keep them.
            for sub in (self.messages_dir, self.state_dir):
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
    # critical sections with an O_EXCL sidecar lock file — portable on Windows
    # and POSIX. Per-agent cursor/threadstate/heartbeat writes are deliberately
    # NOT locked: they are single-writer under the documented one-window-per-
    # agent model; only shared config.json needs this.

    def _read_lock_pid(self, lock: Path) -> int | None:
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("pid") if isinstance(data, dict) else None
        return pid if isinstance(pid, int) else None

    def _break_stale_lock(self, lock: Path) -> bool:
        """Break a lock held by a provably-dead pid; return True if broken.

        The claim is ATOMIC (``os.replace`` the lock onto a per-pid sidecar):
        only one racing stealer wins the rename, the loser's replace raises
        and it re-loops, so two processes can never both break the same lock
        and then both enter the critical section. A lock that is unreadable,
        garbage, our own pid, or held by a LIVE pid is never broken — we wait
        out the timeout instead (no mtime-only breaking)."""
        pid = self._read_lock_pid(lock)
        if pid is None or pid == os.getpid() or _process_alive(pid):
            return False
        claimed = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
        try:
            os.replace(str(lock), str(claimed))
        except OSError:
            return False  # lock vanished or another stealer won the claim
        try:
            os.unlink(str(claimed))
        except OSError:
            pass
        return True

    @contextlib.contextmanager
    def _exclusive_lock(self, lock: Path, *, timeout: float = 10.0,
                        poll: float = 0.05, what: str = "lock"):
        """Hold an exclusive O_EXCL sidecar lock across a read-modify-write.

        Portable on Windows + POSIX (atomic create); breaks a lock held by a
        provably-dead pid (never a live one); times out otherwise. NOT re-entrant.
        Shared by ``_config_lock`` (config.json) and the lead-loop lease lock so
        both get the same battle-tested stale-break behavior."""
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        fd: int | None = None
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if self._break_stale_lock(lock):
                    continue  # broke a dead holder — retry create immediately
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire the {what} at {lock} within {timeout:g}s. "
                        f"If no agent is running, remove the stale lock file."
                    ) from None
                time.sleep(poll)
        try:
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(), "at": _now_iso(), "root": str(self.root),
                }).encode("utf-8"))
            finally:
                os.close(fd)
            yield
        finally:
            # Release RELIABLY. On Windows a concurrent reader (a peer's
            # _break_stale_lock reading the pid) holds the lock file open for a
            # microsecond, and os.unlink of an open file raises PermissionError. If
            # we swallowed that, the lock would orphan with OUR (live) pid - which no
            # waiter can break (live pid) - livelocking them to the timeout. Retry
            # briefly so a colliding read-open never strands the lock.
            for _ in range(100):
                try:
                    lock.unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.01)

    def _config_lock(self, *, timeout: float = 10.0, poll: float = 0.05):
        """Hold an exclusive lock across a config read-modify-write."""
        return self._exclusive_lock(self.dir / "config.lock", timeout=timeout,
                                    poll=poll, what="config lock (another agent may be "
                                    "mid roster-admin)")

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

    def add_agent(self, name: str, *, role: str | None = None,
                  groups: list[str] | None = None) -> dict:
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
        memberships. Leaves its cursor/messages as historical record."""
        with self._config_lock():
            cfg = self.load_config()
            roster = list(cfg.get("agents", []))
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
        """Remove ``name`` from the active roster, roles, groups, and the
        operator_facing slot — in place. Shared by retire/rename. Never touches
        message files."""
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

    def retire_agent(self, name: str, *, reason: str | None = None,
                     renamed_to: str | None = None) -> dict:
        """Retire an active identity to a permanent tombstone (FR-002/003/004).

        ``renamed_to`` links a rename's tombstone to its successor (set by
        :meth:`rename_agent`). Refuses a name that is not currently active.
        """
        with self._config_lock():
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
        with self._config_lock():
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
            # Validate the WHOLE resulting config before writing (fail-closed).
            validate_agent_roster(roster)
            validate_retired(retired, roster)
            if cfg.get("roles"):
                validate_roles(cfg["roles"], roster)
            if cfg.get("groups"):
                validate_groups(cfg["groups"], roster)
            if cfg.get("managed_lead_loop"):
                validate_managed_lead_loop(cfg["managed_lead_loop"], roster)
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
    ) -> Message:
        if not self.initialized():
            raise FileNotFoundError("agenttalk not initialized; run `agenttalk init`.")
        cfg = self.load_config()
        agents = set(cfg.get("agents", []))
        # A retired identity (#19) is removed from the ACTIVE roster, so it
        # already fails this membership check — but give it a tombstone-specific
        # message rather than a confusing "not in registered agents" (FR-004).
        retired = set(self._retired_names(cfg))
        if agents and sender not in agents:
            if sender in retired:
                raise ValueError(
                    f"sender '{sender}' is retired (a tombstone) and cannot "
                    f"send; tombstones are permanent (#19). See `agenttalk roster`."
                )
            raise ValueError(f"sender '{sender}' not in registered agents {sorted(agents)}")
        if agents and recipient not in agents:
            if recipient in retired:
                raise ValueError(
                    f"recipient '{recipient}' is retired (a tombstone) and cannot "
                    f"receive new messages (#19). See `agenttalk roster`."
                )
            raise ValueError(f"recipient '{recipient}' not in registered agents {sorted(agents)}")
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
        meta = dict(meta or {})
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
        _atomic_write_text(path, json.dumps(msg.to_dict(), indent=2, ensure_ascii=False))
        return msg

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
                    failure_class: str, at: str) -> Path:
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
                         if p.is_file() and p.name.endswith(".json")
                         and not p.name.endswith(".deadletter.json"))
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
                if not (p.is_file() and p.name.endswith(".json")
                        and not p.name.endswith(".deadletter.json")):
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
        """The original message bytes for a dead-lettered id, or None."""
        p = self.dead_letter_dir / validate_agent_name(agent) / f"{msg_id}.json"
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
        try:
            cfg = self.load_config()
            # D3 (#19): validate history against the KNOWN roster (active ∪
            # retired) so a retired identity's past messages stay valid.
            roster = self._known_roster(cfg)
        except (ValueError, OSError, FileNotFoundError):
            roster = []
        if not roster:
            # Fail CLOSED: a missing/corrupt roster (load_config raised, or an
            # empty agents list) means there are no valid senders/recipients,
            # so deliver NOTHING — rather than fall through to Message.validate's
            # empty-roster fail-open and deliver forged/off-roster messages
            # (review L). CLI delivery commands already abort earlier on a
            # corrupt config; this makes the store-level contract explicit.
            return []
        require_sig = self.signing_enforced()
        project_id = self.project_id() if require_sig else None
        key: bytes | None = None
        if require_sig:
            try:
                key = _signing.load_key(project_id)
            except (FileNotFoundError, OSError, ValueError):
                key = None  # key vanished between check and load — refuse
        valid, _ = self._scan_messages(since_id=since_id)
        out: list[Message] = []
        for m in valid:
            try:
                m.validate(roster)
            except ValueError:
                continue
            if require_sig:
                if key is None:
                    continue  # policy on but no key — refuse everything
                try:
                    _signing.verify_message(
                        m.to_dict(), key, expected_key_id=project_id,
                    )
                except ValueError:
                    continue
            out.append(m)
        # Restore the documented "sorted by id (chronological)" contract:
        # _scan_messages_with_paths yields raw filesystem-iteration (filename)
        # order, which equals id order ONLY because stem==id is now enforced
        # above. Sort explicitly so delivery, cursor advance, and thread
        # replay are correct even if that invariant is ever relaxed (review H1).
        out.sort(key=lambda m: m.id)
        # Defensive dedupe by id: stem==id + unique filenames make duplicate
        # ids structurally impossible today, but double-delivery would be a
        # silent correctness bug if that ever changed, so guard it cheaply.
        deduped: list[Message] = []
        seen_ids: set[str] = set()
        for m in out:
            if m.id in seen_ids:
                continue
            seen_ids.add(m.id)
            deduped.append(m)
        return deduped

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

    def write_waiting(self, agent: str, info: dict) -> None:
        """Stamp .agenttalk/state/<agent>.waiting with a JSON liveness record.

        Overwrites any existing marker (a fresh `wait` supersedes a
        stale one). Best-effort: callers treat any write failure as
        non-fatal since this is observability-only. (Shared write_text, with the
        in-sandbox direct-write fallback - see _atomic.write_text.)
        """
        p = self.state_dir / f"{agent}.waiting"
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
        p = self.state_dir / f"{agent}.waiting"
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

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

    def _lead_loop_lease_lock(self, agent: str):
        """Exclusive per-agent lock serializing acquire/renew/release/steal so the
        read-decide-write is ATOMIC - two contenders can never both 'acquire' an
        empty lease (reviewer-1 blocker). Reuses the O_EXCL stale-break machinery."""
        return self._exclusive_lock(
            self.state_dir / f"{validate_agent_name(agent)}.lead-loop-lease.lock",
            what="lead-loop lease lock")

    def read_lead_loop_lease(self, agent: str) -> dict | None:
        """Return the parsed lease dict, or None if absent/corrupt. Never raises
        (a torn write reads as None -> treated as 'no lease', fail-safe)."""
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
        return data if isinstance(data, dict) else None

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
            "lead_loop": True,
            "managed": True,
        })

    def _lease_stealable(self, existing: dict, agent: str, *, now: float,
                         heartbeat_stale_after: float | None) -> bool:
        """A managed lease is STEALABLE when the owner is gone, so a replacement
        controller can recover WITHOUT waiting out the TTL:

          - a CONFIRMED-DEAD owner (a DEFINITIVE OS not-running signal, via the
            tri-state :func:`_process_liveness`) is stealable IMMEDIATELY, regardless
            of expiry. The process is gone and nothing will ever renew the lease, so
            gating recovery on TTL would strand the team mailbox for up to a full TTL
            (reviewer-1 release-blocker: a dead owner within TTL was unarmed AND
            unguarded yet un-stealable - a down-but-unrecoverable limbo).
          - an ALIVE *or* UNKNOWN owner is stealable only once the lease is EXPIRED
            *and* its heartbeat is stale (a stuck controller). A long HEALTHY turn
            (within TTL, or heartbeat fresh) is NEVER stolen; it renews instead. An
            UNKNOWN probe (access-denied / exception / ambiguous) is treated as
            probably-ALIVE and takes this conservative path - so a fail-quiet probe
            can NEVER immediate-steal a live controller (codex blocker; lead D-12
            Option A). Only a DEFINITIVE death authorizes the immediate steal.

        This is the EXACT complement of ``lead_loop_state``'s ``armed`` (= NOT
        confirmed-dead AND NOT (expired AND heartbeat-stale)): for a present managed
        lease, not-stealable == armed, for EVERY case (alive, dead, OR unknown).
        Steal is gated on the CONFIGURED managed_lead_loop flag (not just the lease's
        own field) so a MANUAL chat identity is NEVER auto-stolen even if a stray
        lease file exists for it."""
        if not (existing.get("managed") and self.is_managed_lead_loop(agent)):
            return False
        if _process_liveness(existing.get("owner_pid")) == PROC_DEAD:
            return True  # confirmed dead -> recover now, no TTL wait
        # ALIVE or UNKNOWN (probably-alive): only a stuck owner past TTL with a stale
        # heartbeat is stealable; a within-TTL / heartbeating owner is never stolen.
        expires_at = existing.get("expires_at")
        if not isinstance(expires_at, (int, float)) or now <= expires_at:
            return False
        stale_after = (heartbeat_stale_after if heartbeat_stale_after is not None
                       else ACTIVE_WITHIN_SECONDS)
        hb = self.read_heartbeat(agent)
        hb_stale = hb is None or (now - hb.timestamp()) > stale_after
        return hb_stale  # expired AND heartbeat-stale = stuck -> stealable

    def acquire_lead_loop_lease(self, agent: str, *, owner_pid: int,
                                ttl_seconds: float | None = None,
                                now: float | None = None,
                                session_id: str | None = None,
                                lease_id: str | None = None,
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
            lease = {
                "schema_version": 1, "managed": True, "mode": LEAD_LOOP_MODE,
                "agent": agent, "owner_pid": int(owner_pid), "owner_start": keep_start,
                "session_id": session_id, "lease_id": lid,
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

    def lead_loop_active_owner(self, agent: str) -> dict | None:
        """Return the lease unless the owner is CONFIRMED dead - the single-consumer
        guard's 'is the mailbox owned' test. The mailbox is protected while the owner
        is ALIVE *or* UNKNOWN (an uncertain probe is treated as probably-alive, so an
        external consumer never races a possibly-live controller); only a DEFINITIVE
        not-running signal (tri-state :func:`_process_liveness` == dead) yields None
        (orphaned lease -> recoverable, not guarded). Independent of expiry: a long
        healthy turn (lease momentarily expired) still owns the mailbox. CONFIG-GATED
        on the managed flag (mirrors _lease_stealable): a stray lease file for a
        MANUAL identity never guards its mailbox, so the store primitive is safe on
        its own and does not rely on the CLI guard's config short-circuit. Uses the
        same tri-state as steal/armed so the three never disagree (lead D-12)."""
        if not self.is_managed_lead_loop(agent):
            return None
        lease = self.read_lead_loop_lease(agent)
        if not lease:
            return None
        if _process_liveness(lease.get("owner_pid")) == PROC_DEAD:
            return None  # confirmed-dead owner -> orphaned -> not guarded
        return lease

    def lead_loop_state(self, agent: str, *, now: float | None = None,
                        heartbeat_stale_after: float | None = None) -> dict:
        """Visibility snapshot for ``agent`` (status/doctor/supervisor). Returns
        {managed, present, owner_pid, owner_alive, owner_liveness, expired,
        heartbeat_stale, armed, reason}. ``armed`` (managed health) = a present lease
        that is NOT stealable, i.e. NOT confirmed-dead AND NOT (expired AND
        heartbeat-stale). It uses the tri-state :func:`_process_liveness`, mirroring
        the steal predicate (_lease_stealable) and the guard (lead_loop_active_owner)
        EXACTLY, so the detector flags precisely the states where another controller
        could legitimately take over: no lease, a CONFIRMED-dead owner, or a lease
        that is expired AND heartbeat-stale. A healthy long turn (within TTL) is
        armed; an expired-but-heartbeating owner is armed (it renews on its next
        cadence); a fresh-lease owner whose heartbeat merely lapsed (within TTL) is
        armed; an UNKNOWN-liveness owner (uncertain probe) is treated as probably-
        alive -> armed within TTL (never a false unarmed from a fail-quiet probe -
        codex blocker / lead D-12 Option A). Only a confirmed-dead owner or the
        BOTH-stale case is unarmed. ``owner_alive`` is True only for a CONFIRMED-live
        probe; ``owner_liveness`` carries the raw tri-state (alive/dead/unknown)."""
        now = now if now is not None else time.time()
        stale_after = (heartbeat_stale_after if heartbeat_stale_after is not None
                       else ACTIVE_WITHIN_SECONDS)
        lease = self.read_lead_loop_lease(agent)
        hb = self.read_heartbeat(agent)
        hb_stale = hb is None or (now - hb.timestamp()) > stale_after
        st = {
            "managed": self.is_managed_lead_loop(agent),
            "present": bool(lease),
            "owner_pid": lease.get("owner_pid") if lease else None,
            "owner_alive": False, "owner_liveness": None, "expired": None,
            "heartbeat_stale": hb_stale, "armed": False, "reason": "",
        }
        if not lease:
            st["reason"] = "no lease"
            return st
        liveness = _process_liveness(lease.get("owner_pid"))
        st["owner_alive"] = liveness == PROC_ALIVE
        st["owner_liveness"] = liveness
        exp = lease.get("expires_at")
        st["expired"] = (not isinstance(exp, (int, float))) or now > exp
        # armed = NOT confirmed-dead AND NOT (expired AND heartbeat-stale). This is the
        # EXACT complement of _lease_stealable for EVERY case (alive, dead, OR unknown):
        # a CONFIRMED-dead owner is unarmed here AND stealable there; an UNKNOWN probe is
        # probably-alive -> armed here AND not-immediately-stolen there. So the detector,
        # the steal predicate, and the guard can never disagree (lead D-12 Option A). A
        # heartbeat that merely lapsed on a within-TTL lease (a long healthy turn) is
        # STILL armed - only an expired AND heartbeat-stale lease is a genuinely down
        # controller (lead P2 - the prior hb-only rule false-ERRORed at 120s, TTL 900s).
        if liveness == PROC_DEAD:
            st["reason"] = "owner confirmed dead"
        elif st["expired"] and hb_stale:
            st["reason"] = "lease expired and heartbeat stale"
        else:
            st["armed"] = True
            if liveness == PROC_UNKNOWN:
                st["reason"] = "armed (owner liveness unknown, treated as alive)"
            elif st["expired"]:
                st["reason"] = "armed (lease expired, heartbeat fresh, pending renewal)"
            elif hb_stale:
                st["reason"] = "armed (heartbeat stale, lease within TTL)"
            else:
                st["reason"] = "armed"
        return st

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
            marker = self.read_waiting(agent)
            if not marker:
                return False
            pid = marker.get("pid")
            if not isinstance(pid, int) or pid == self_pid:
                return False
            if _process_alive(pid):
                return False
            self.clear_waiting(agent)
            return True
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
        if hb is not None and (now - hb.timestamp()) <= ACTIVE_WITHIN_SECONDS:
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
        """Atomically write a queued ephemeral launch request marker."""
        from agenttalk import ephemeral as _eph
        rid = payload.get("request_id") if isinstance(payload, dict) else None
        if not _eph.is_safe_id(rid):
            raise ValueError(f"unsafe launch request_id {rid!r}")
        data = dict(payload)
        data.setdefault("state", _eph.STATE_QUEUED)
        _atomic_write_text(self._launch_request_path(rid),
                           json.dumps(data, indent=2, ensure_ascii=False))

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

    def update_launch_request(self, request_id: str, updates: dict) -> dict | None:
        """Request-id checked marker update. None means absent/superseded."""
        with self._config_lock():
            marker = self.read_launch_request(request_id)
            if not marker:
                return None
            marker.update(dict(updates))
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
