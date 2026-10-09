"""Isolated accounts and a controllable clock for the recurring capacity checks."""

import getpass
import json
import os
from datetime import datetime, timezone

from agenttalk import capacity as cap, cli

AT = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
OWN = 12
OTHER = 88


class Clock(datetime):
    instant = AT

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


def isolate(monkeypatch, root):
    for key in ("AGENTTALK_ROOT", "AGENTTALK_SELF", "CLAUDE_CONFIG_DIR", "CLAUDECODE",
                "CODEX_HOME", "CODEX_THREAD_ID"):
        monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(root / "caller"))
    monkeypatch.setattr(getpass, "getuser", lambda: "synthetic")
    monkeypatch.setattr(Clock, "instant", AT)
    monkeypatch.setattr(cap, "datetime", Clock)
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cap.tempfile, "gettempdir", lambda: str(root))


def statusline(home, percent, session_id="target-session"):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "statusline-last-input.json"
    path.write_text(json.dumps({
        "session_id": session_id,
        "rate_limits": {"five_hour": {"used_percentage": percent}},
        "context_window": {"used_percentage": percent, "context_window_size": 1000,
                           "current_usage": {"input_tokens": percent * 10}},
    }), encoding="utf-8")
    os.utime(path, (AT.timestamp(), AT.timestamp()))
    return path


def rollout(home, percent, thread):
    path = home / "sessions" / f"rollout-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "session_meta", "payload": {"id": thread}},
        {"timestamp": AT.isoformat(), "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"primary": {"used_percent": percent}},
            "info": {"model_context_window": 1000, "last_token_usage": {"input_tokens": percent * 10}},
        }},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def stream(binding, percent=OWN):
    return cap.claude_stream_reading(
        {"rateLimitType": "five_hour", "status": "allowed", "utilization": percent / 100},
        {"binding": binding}, observed_at=AT.isoformat())


def directory_link(link, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_directory_link(link):
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()
