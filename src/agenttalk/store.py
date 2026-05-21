"""On-disk message store.

Layout under <root>/.agenttalk/:
    config.json            session config + agent roster
    messages/<id>.json     one file per message, lexicographically sorted by id
    state/<agent>.cursor   last message id this agent has acknowledged
    sessions/              exported transcripts

Message id format: ``YYYYMMDD-HHMMSS-uuuuuu-XXXX`` where the suffix is a
4-char random tag to avoid collisions when two messages land in the same
microsecond. Lexicographic order == chronological order.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from agenttalk._atomic import write_text as _atomic_write_text

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
    "wake",
    "end",
})

# Agent names are interpolated directly into filesystem paths
# (cursors, heartbeats), so they must be portable identifiers — not
# arbitrary user input. Allow alphanumerics plus dot / underscore /
# hyphen, must start with an alphanumeric, max 64 chars. Note: we
# deliberately use `\A...\Z` rather than `^...$` because Python's
# `$` anchor matches immediately before a trailing newline, which
# would let `"claude\n"` slip through into a state filename.
_AGENT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


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
        for field in ("id", "ts"):
            if field not in data:
                raise ValueError(f"missing required field {field!r}")
        for field in ("id", "ts"):
            if not isinstance(data[field], str) or not data[field]:
                raise ValueError(
                    f"field {field!r} must be a non-empty string, "
                    f"got {type(data[field]).__name__}"
                )
        for field in ("kind", "subject", "body"):
            if field in data and not isinstance(data[field], str):
                raise ValueError(
                    f"field {field!r} must be a string if present, "
                    f"got {type(data[field]).__name__}"
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
        }
        _atomic_write_text(self.config_path, json.dumps(cfg, indent=2))
        for a in agents:
            cur = self.state_dir / f"{a}.cursor"
            if not cur.exists():
                _atomic_write_text(cur, "")
        return cfg

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
            )
        return cfg

    # --------------------------------------------------------------- writing

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        body: str,
        kind: str = "message",
        subject: str = "",
        meta: dict | None = None,
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
        failed (JSON parse, missing fields, wrong types, OR
        schema/roster validation). Surfaces what `messages_for()`
        silently skipped — so tampering is visible rather than
        invisible. Used by `agenttalk status` and `agenttalk doctor`.
        """
        try:
            cfg = self.load_config()
        except (ValueError, OSError, FileNotFoundError):
            roster = []
        else:
            roster = cfg.get("agents", []) or []
        valid, parse_failures = self._scan_messages()
        out = list(parse_failures)
        for m in valid:
            try:
                m.validate(roster)
            except ValueError as e:
                out.append((m.id, str(e)))
        return out

    def messages_for(self, agent: str, *, since_id: str | None = None) -> list[Message]:
        """Return validated messages addressed to ``agent``.

        Silently skips messages that fail schema/roster validation so
        callers (wait, recv) never act on malformed input. Use
        ``list_invalid_messages()`` to see what was skipped.
        """
        try:
            cfg = self.load_config()
            roster = cfg.get("agents", []) or []
        except (ValueError, OSError, FileNotFoundError):
            roster = []
        valid, _ = self._scan_messages()
        msgs: list[Message] = []
        for m in valid:
            if m.recipient != agent:
                continue
            try:
                m.validate(roster)
            except ValueError:
                continue
            if since_id and m.id <= since_id:
                continue
            msgs.append(m)
        return msgs

    def unread_for(self, agent: str) -> list[Message]:
        return self.messages_for(agent, since_id=self.cursor(agent))

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


# --------------------------------------------------------- helpers (module)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id() -> str:
    now = datetime.now(timezone.utc)
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
    return now.strftime("%Y%m%d-%H%M%S-%f") + "-" + suffix


def _new_session_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def find_root(start: Path | None = None) -> Path:
    """Find the project root containing a `.agenttalk/` dir, searching upward.

    Falls back to the start dir (or CWD) so `init` can create a fresh store.
    """
    start = Path(start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        if (d / DIRNAME).is_dir():
            return d
    return start
