"""How large an agent's current conversation is, and whether that deserves a warning (#475).

Read-only and advisory. Every turn of a long-lived wrapped agent sends its whole
conversation again, so a long conversation makes every turn dearer. This module only
*describes* the size; it never restarts, stops or changes an agent and sends no message.

Two figures, never mixed: the context size the agent's CLI reported on its last turn
(``capacity`` snapshot, ``context_tokens``) where a reading exists, else the number of
turns the agent has run in total (the wrapper session record's ``turns``).

The turn counter is NOT tied to one conversation: it counts every successful turn, a fresh
start does not reset it (``wrapper/session.py``, ``wrapper/run.py``) and other code reads
it, so it is not reset here. The figure is therefore labelled ``turns_total`` and the
warning says it can include earlier conversations; only the token reading describes the
current conversation. (A later change can subtract the count recorded at a fresh start.)

Defaults (``DEFAULT_WARN_TOKENS`` / ``DEFAULT_WARN_TURNS``) and how they were chosen:
  * tokens: Claude reports a 200000-token window (a 1000000-token variant exists) and Codex
    reports 258400 in its rollouts. Auto-compaction starts near the top of the window, so the
    warning sits well below it: 150000 for Claude (75% of 200000) and 190000 for Codex
    (about 74% of 258400).
  * turns: the incident behind #475 was a conversation of 830 turns; healthy desktop
    agents sit at 170 to 230 in their current conversations. 300 stays quiet for them and
    fires long before 830. Because the counter is cumulative it can fire for an agent that
    has since been started fresh; the text says so.
They can be changed in ``supervisor.json`` with ``conversation_warn_tokens`` and
``conversation_warn_turns`` (a number for every CLI, or ``{"claude": n, "codex": n}``).
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

BASIS_TOKENS = "context_tokens"
BASIS_TURNS = "turns_total"

DEFAULT_WARN_TOKENS = {"claude": 150_000, "codex": 190_000}
DEFAULT_WARN_TURNS = {"claude": 300, "codex": 300}
_CONFIG_TOKENS_KEY = "conversation_warn_tokens"
_CONFIG_TURNS_KEY = "conversation_warn_turns"
_FALLBACK_CLI = "claude"


def _whole(value: object) -> int | None:
    """A whole number (an int, or a float with no fraction), else None. Never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _positive_int(value: object) -> int | None:
    """A limit: a positive whole number (0.5 is refused, not turned into 0)."""
    whole = _whole(value)
    return whole if whole is not None and whole > 0 else None


def _measurement(value: object) -> int | None:
    """A reading or a count: a whole number from 0 up. Zero is a real value."""
    whole = _whole(value)
    return whole if whole is not None and whole >= 0 else None


def _limit(raw: object, defaults: dict[str, int], cli: str) -> int:
    """Configured limit for ``cli``; any unusable setting falls back to the default."""
    if isinstance(raw, dict):
        value = _positive_int(raw.get(cli))
    else:
        value = _positive_int(raw)
    return value if value is not None else defaults.get(cli, defaults[_FALLBACK_CLI])


def _is_cli(value: object) -> bool:
    """True only for the two known tool names. A list or object is simply not one (it must
    not be used as a dict key: that raises TypeError)."""
    return isinstance(value, str) and value in DEFAULT_WARN_TOKENS


def load_settings(store_dir: Path) -> dict:
    """The two warning settings and the per-agent ``cli`` names from ``supervisor.json``.

    ``{}`` when absent or unreadable. Reads the file the way the supervisor does
    (``utf-8-sig``, so a byte-order mark is accepted) and never raises: a broken file must
    not hide the status."""
    try:
        data = json.loads(Path(store_dir, "supervisor.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: data[k] for k in (_CONFIG_TOKENS_KEY, _CONFIG_TURNS_KEY) if k in data}
    agents = data.get("agents")
    if isinstance(agents, dict):
        out["agent_cli"] = {name: entry["cli"] for name, entry in agents.items()
                            if isinstance(entry, dict) and _is_cli(entry.get("cli"))}
    return out


def resolve_cli(agent: str, settings: dict | None, health: dict | None,
                capacity_snap: dict | None) -> str | None:
    """``"claude"`` / ``"codex"`` or None: the supervisor entry's ``cli``, else the health
    record's, else the capacity source prefix, else the agent-name prefix. ONE rule for
    ``status`` and the dashboard, so they cannot disagree."""
    candidates = [((settings or {}).get("agent_cli") or {}).get(agent),
                  health.get("cli") if isinstance(health, dict) else None]
    src = capacity_snap.get("source") if isinstance(capacity_snap, dict) else None
    if isinstance(src, str):
        candidates.append(src.split("_", 1)[0])
    candidates.append(agent.split("-", 1)[0])
    return next((c for c in candidates if _is_cli(c)), None)


def _read_before(observed_at: object, fresh_at: object) -> bool:
    """True when a context reading was observed before the agent's last fresh start (or its
    time cannot be read): it describes the previous conversation. False without a fresh start."""
    if not isinstance(fresh_at, str) or not fresh_at:
        return False
    try:
        start = datetime.fromisoformat(fresh_at.replace("Z", "+00:00"))
        seen = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        return seen < start
    except (TypeError, ValueError):
        return True


def reading_view(runtime: dict | None) -> dict | None:
    """The wrapper's own reading of the current conversation, from ``read_wrapper_runtime``:
    ``{tokens, window, percent, at}`` or None. The percent exists only when the window was
    stated by the stream. The ONE place the figure is shaped; status, the dashboard and the
    checkpoint all call it."""
    conv = runtime.get("conversation") if isinstance(runtime, dict) else None
    if not isinstance(conv, dict) or _measurement(conv.get("tokens")) is None:
        return None
    tokens, window = conv["tokens"], _positive_int(conv.get("window"))
    view = {"tokens": tokens, "window": window, "at": conv.get("at"),
            "percent": round(tokens * 100 / window) if window else None}
    if conv.get("current") is False:
        view["current"] = False  # present only when set: the saved reading may be out of date
    return view


def conversation_size(cli: str | None, capacity_snap: dict | None, turns: object,
                      settings: dict | None = None, fresh_at: str | None = None,
                      reading: dict | None = None) -> dict | None:
    """``{basis, value, limit, warn, cli}`` or None when neither figure is known.

    ``capacity_snap`` is a ``capacity.current_view`` result (so an expired reading shows
    ``context_tokens`` as None and falls back to turns). ``turns`` may be None/absent.
    ``fresh_at`` is when the agent last started fresh (``fresh_request_at``): a token reading
    observed before it belongs to the old conversation and is ignored. ``reading`` (see
    :func:`reading_view`) is the wrapper's own figure for the current conversation and comes first."""
    settings = settings or {}
    cli_key = cli if cli in DEFAULT_WARN_TOKENS else _FALLBACK_CLI
    if reading is not None:
        limit = _limit(settings.get(_CONFIG_TOKENS_KEY), DEFAULT_WARN_TOKENS, cli_key)
        saved = reading.get("current") is False  # an obsolete size whose end could not be saved: no warning
        return {"basis": BASIS_TOKENS, "value": reading["tokens"], "limit": limit,
                "warn": reading["tokens"] > limit and not saved, "cli": cli_key,
                "scope": "saved" if saved else "conversation",
                "at": reading["at"], "window": reading["window"], "percent": reading["percent"]}
    tokens = _measurement(capacity_snap.get("context_tokens")) if isinstance(capacity_snap, dict) else None
    if tokens is not None and _read_before(capacity_snap.get("observed_at"), fresh_at):
        tokens = None
    if tokens is not None:
        limit = _limit(settings.get(_CONFIG_TOKENS_KEY), DEFAULT_WARN_TOKENS, cli_key)
        return {"basis": BASIS_TOKENS, "value": tokens, "limit": limit,
                "warn": tokens > limit, "cli": cli_key}
    count = _measurement(turns)
    if count is None:
        return None
    limit = _limit(settings.get(_CONFIG_TURNS_KEY), DEFAULT_WARN_TURNS, cli_key)
    return {"basis": BASIS_TURNS, "value": count, "limit": limit,
            "warn": count > limit, "cli": cli_key}


def format_size(size: dict) -> str:
    """One short phrase for a terminal row, saying which figure it is."""
    if size.get("scope") == "saved":
        return (f"conversation={size['value']:,} tokens (saved reading, may be out of date; "
                f"read {size['at']})")
    if size.get("scope") == "conversation":
        pct = (f"{size['percent']}% of {size['window']:,}, " if size.get("percent") is not None
               else "window unknown, " if size.get("window") is None else "")
        text = f"conversation={size['value']:,} tokens in this conversation ({pct}read {size['at']})"
        note = "every turn re-sends it"
    elif size["basis"] == BASIS_TOKENS:
        text = f"conversation={size['value']:,} tokens of context"
        note = "every turn re-sends it"
    else:
        text = f"conversation={size['value']:,} turns in total"
        note = "may count earlier conversations; a fresh start does not reset it"
    if size["warn"]:
        text += f" WARN(over {size['limit']:,}: {note})"
    return text
