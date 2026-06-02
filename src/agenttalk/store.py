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

import json
import re
import secrets
import shutil
import string
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk import signing as _signing

DIRNAME = ".agenttalk"
_ID_ALPHABET = string.ascii_letters + string.digits

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
    # Control-plane kind: peer is still drafting a real reply. Receivers
    # treat these as a deadline-extension signal in `agenttalk wait` —
    # they do not surface as a returned reply. Added in 0.8.0 to fix
    # "reply landed seconds after wait timed out" sharp-edge.
    "composing",
})

# Kinds the bus uses to signal flow control rather than carry agent
# content. They are still persisted (so transcripts and the dashboard
# can show them for audit), but `agenttalk wait` does not return them
# as a reply and `agenttalk recv` filters them out of the default view.
CONTROL_KINDS = frozenset({"composing"})

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
        return cfg

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
        if agents and sender not in agents:
            raise ValueError(f"sender '{sender}' not in registered agents {sorted(agents)}")
        if agents and recipient not in agents:
            raise ValueError(f"recipient '{recipient}' not in registered agents {sorted(agents)}")
        # Reject unknown kinds at WRITE time so the sender sees an
        # immediate error rather than a silent receive-side skip.
        # Without this, `agenttalk send --kind typo` would exit 0 +
        # the message would be invisible to the peer's wait/recv.
        if kind not in KNOWN_KINDS:
            raise ValueError(
                f"unknown kind {kind!r} (allowed: {sorted(KNOWN_KINDS)})"
            )
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

    def _scan_messages(self) -> tuple[list[Message], list[tuple[str, str]]]:
        """Read every file in messages/ once, separating valid messages
        from invalid ones. Returns (valid, invalid) where invalid is
        [(file_stem_or_id, reason)].

        This is the canonical read path — never construct a Message
        from disk JSON without going through here. Catches JSON
        parse errors, shape/type errors, and missing fields *before*
        downstream callers can crash on `data["id"]` or compare a
        numeric id against a string cursor.
        """
        valid: list[Message] = []
        invalid: list[tuple[str, str]] = []
        if not self.messages_dir.exists():
            return valid, invalid
        for p in sorted(self.messages_dir.iterdir()):
            if p.suffix != ".json":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError as e:
                invalid.append((p.stem, f"cannot read file: {e}"))
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                invalid.append((p.stem, f"invalid JSON: {e}"))
                continue
            try:
                msg = Message.from_raw(data)
            except ValueError as e:
                ident = data.get("id") if isinstance(data, dict) else None
                if not isinstance(ident, str) or not ident:
                    ident = p.stem
                invalid.append((ident, str(e)))
                continue
            valid.append(msg)
        return valid, invalid

    def all_messages(self) -> list[Message]:
        """Return all parseable + schema-valid messages.

        Roster validation is applied in ``messages_for``; this method
        returns everything that constructed cleanly, so transcript
        export still sees messages from old sessions whose agents are
        no longer in the current roster.
        """
        valid, _ = self._scan_messages()
        return valid

    def list_invalid_messages(self) -> list[tuple[str, str]]:
        """Return [(id_or_stem, reason)] for every message file that
        failed parse, schema, roster, OR signature validation.
        Surfaces everything ``messages_for()`` silently skipped so
        tampering is visible rather than invisible. Used by
        ``agenttalk status`` and ``agenttalk doctor``.
        """
        try:
            cfg = self.load_config()
        except (ValueError, OSError, FileNotFoundError):
            cfg = {}
        roster = cfg.get("agents", []) or []
        require_sig = self.signing_enforced()
        project_id = self.project_id() if require_sig else None
        key: bytes | None = None
        if require_sig:
            try:
                key = _signing.load_key(project_id)
            except (FileNotFoundError, OSError, ValueError):
                key = None
        valid, parse_failures = self._scan_messages()
        out = list(parse_failures)
        for m in valid:
            try:
                m.validate(roster)
            except ValueError as e:
                out.append((m.id, str(e)))
                continue
            if require_sig:
                if key is None:
                    out.append((m.id, "signatures enforced but no key file is loadable"))
                    continue
                try:
                    _signing.verify_message(
                        m.to_dict(), key, expected_key_id=project_id,
                    )
                except ValueError as e:
                    out.append((m.id, str(e)))
        return out

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

    def _validated_messages(self) -> list[Message]:
        """Shared trust gate behind ``messages_for`` and ``valid_messages``.

        Applies schema/roster validation and (when enforced) HMAC
        signature verification to every scanned message, returning the
        survivors in id order. No recipient/since filtering — callers
        layer that on top.
        """
        try:
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
        except (ValueError, OSError, FileNotFoundError):
            roster = []
        require_sig = self.signing_enforced()
        project_id = self.project_id() if require_sig else None
        key: bytes | None = None
        if require_sig:
            try:
                key = _signing.load_key(project_id)
            except (FileNotFoundError, OSError, ValueError):
                key = None  # key vanished between check and load — refuse
        valid, _ = self._scan_messages()
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
        return out

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
        for m in self._validated_messages():
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
        return p.read_text(encoding="utf-8").strip()

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
        for correctness.
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
        non-fatal since this is observability-only.
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

    def clear_waiting(self, agent: str) -> None:
        """Remove the waiting marker if present. Best-effort, never raises."""
        p = self.state_dir / f"{agent}.waiting"
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# --------------------------------------------------------- helpers (module)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


_id_lock = threading.Lock()
_last_id_dt: datetime | None = None


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
    """Find the project root containing a `.agenttalk/` dir, searching upward.

    Falls back to the start dir (or CWD) so `init` can create a fresh store.
    """
    start = Path(start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        if (d / DIRNAME).is_dir():
            return d
    return start
