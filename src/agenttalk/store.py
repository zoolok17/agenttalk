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
import secrets
import string
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DIRNAME = ".agenttalk"
_ID_ALPHABET = string.ascii_letters + string.digits


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
        return json.loads(self.config_path.read_text(encoding="utf-8"))

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

    def all_messages(self) -> list[Message]:
        if not self.messages_dir.exists():
            return []
        out: list[Message] = []
        for p in sorted(self.messages_dir.iterdir()):
            if p.suffix != ".json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append(Message.from_dict(data))
        return out

    def messages_for(self, agent: str, *, since_id: str | None = None) -> list[Message]:
        msgs = [m for m in self.all_messages() if m.recipient == agent]
        if since_id:
            msgs = [m for m in msgs if m.id > since_id]
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


# --------------------------------------------------------- helpers (module)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id() -> str:
    now = datetime.now(timezone.utc)
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
    return now.strftime("%Y%m%d-%H%M%S-%f") + "-" + suffix


def _new_session_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write atomically via temp file + os.replace (atomic on Windows and POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find_root(start: Path | None = None) -> Path:
    """Find the project root containing a `.agenttalk/` dir, searching upward.

    Falls back to the start dir (or CWD) so `init` can create a fresh store.
    """
    start = Path(start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        if (d / DIRNAME).is_dir():
            return d
    return start
