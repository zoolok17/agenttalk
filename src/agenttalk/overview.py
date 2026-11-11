"""``agenttalk overview``: one read-only screen with what the dashboard's Overview shows.

It renders ONLY what the dashboard's own builders return (``web.build_state``,
``web.build_attention``, ``web.build_gates``). It derives nothing of its own: a field the
builders leave out is left out here, and the words below only translate values the builders
already decided. So the terminal and the dashboard cannot drift apart.

Read-only: it never moves an inbox cursor, writes to the store, wakes an agent or calls a model.
Spend (the machine-wide ledger) is shown only when asked for (``--enable-budget``, the same
name and default as the dashboard's switch), because any seat can run this command.
Output is plain ASCII so it prints the same on Windows, Linux and macOS.
"""
from __future__ import annotations

import shutil
import textwrap
from typing import Any

from agenttalk import supervisor, web
from agenttalk.wrapper import usage_park

MAX_ATTENTION = 5
MAX_THREADS = 6

_HEALTH_WORDS = {
    "idle_waiting": "waiting for work",
    "working_turn": "working",
    "working_silent": "working (quiet for a while)",
    "stuck_suspected": "may be stuck",
    "rate_limited_or_outage": "held up by a provider limit or outage",
    "degraded_output": "producing poor output",
    "errored_poison": "stopped on a message it cannot handle",
    "errored_ambiguous": "stopped after an unclear error",
    "crashed_or_exited": "not running",
    "unknown": "status unknown",
}


def gather(store: Any, *, label: str, enable_budget: bool = False) -> dict:
    """The three dashboard payloads for one project (plus the spend feed only when asked)."""
    desc = web.RootDescriptor(store=store, label=label)
    state = web.build_state([desc])["roots"][0]
    agents = state.get("agents") if isinstance(state.get("agents"), list) else None
    data = {
        "state": state,
        "attention": web.build_attention(desc, agents),
        "gates": web.build_gates(desc),
    }
    if enable_budget:
        from agenttalk.budget import BudgetFeed
        data["budget"] = BudgetFeed().get()
    return data


def _attention_total(attention: dict) -> int | None:
    """The builder's own count of everything needing attention: ``active_count`` (counted before
    similar items are grouped for display), with ``count`` (the rows shown) only as a fallback."""
    for key in ("active_count", "count"):
        value = attention.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _owners(value: object) -> str:
    """Who owes the next move: one name, or several (a broadcast) joined into a readable list."""
    names = [_text(v) for v in value if isinstance(v, str) and v] if isinstance(value, list) else []
    if isinstance(value, str) and value:
        names = [_text(value)]
    if not names:
        return "nobody's"
    return names[0] + "'s" if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] + " (each)"


def _age(seconds: object) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        return "unknown age"
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} d"


def _ago(seconds: object) -> str:
    """The age as a complete phrase for "seen ...": "just now", "5 min ago". `_age` alone is only the
    length of time, so "just now" never gets a second "ago" after it."""
    span = _age(seconds)
    if span == "just now":
        return span
    if span == "unknown age":
        return "at an unknown time"
    return f"{span} ago"


def _safe(text: str) -> str:
    """Only printable ASCII survives. Everything else (escape and other control characters, a
    DEL, any non-ASCII character) becomes ``?``, so no text from a message or a config can move
    the cursor, clear the screen or otherwise steer the operator's terminal."""
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)


def _text(value: object) -> str:
    """One line of plain, printable ASCII (the builders' text may hold any character)."""
    return _safe(" ".join(str(value).split()))


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _health_words(health: dict, verdict: object) -> str:
    """The agent's state in words, ranked the way ``agenttalk status`` and the dashboard rank it.

    The wrapper reports its own health and cannot see its own CLI child die, so an independent
    supervisor verdict that does not confirm the child healthy is the primary status, and the
    wrapper's report is only a labelled aside. A stale report that was normalised to "unknown"
    keeps what it last said, labelled as old, instead of reading as no information."""
    state = health.get("state")
    wrapper = _HEALTH_WORDS.get(state) if isinstance(state, str) else None
    if state == "unknown" and isinstance(health.get("last_known_state"), str):
        last = _HEALTH_WORDS.get(health["last_known_state"])
        if last:
            wrapper = f"no recent report (it last said: {last})"
    wrapper = wrapper or "status unknown"
    vstate = verdict.get("state") if isinstance(verdict, dict) else None
    if isinstance(vstate, str) and not supervisor.cli_child_verdict_is_healthy(vstate):
        return f"supervisor says {_text(vstate.replace('_', ' ').lower())} (the wrapper itself reports: {wrapper})"
    if isinstance(vstate, str):
        return f"{wrapper}, supervisor: {_text(vstate.replace('_', ' ').lower())}"
    return wrapper


def _seat_line(agent: dict) -> str:
    health = agent.get("health") if isinstance(agent.get("health"), dict) else {}
    parts = [_health_words(health, agent.get("cli_child_verdict"))]
    if health.get("stale") is True and health.get("state") != "unknown":
        parts.append("(that status is old)")
    park = usage_park.park_text(agent.get("usage_limit_park"))
    if park:  # the shared wording of every console (provider wait, usage limit, wrapper not responding)
        parts.append(_text(park))
    if isinstance(agent.get("last_seen_age_seconds"), (int, float)):
        parts.append(f"seen {_ago(agent['last_seen_age_seconds'])}")
    cap = agent.get("capacity")
    if isinstance(cap, dict):
        for key, name in (("rate_used_pct", "limit used"), ("context_used_pct", "memory used")):
            if isinstance(cap.get(key), (int, float)) and not isinstance(cap.get(key), bool):
                parts.append(f"{name} {cap[key]:.0f}%")
    size = agent.get("conversation_size")
    if isinstance(size, dict) and size.get("warn") is True:
        parts.append("conversation getting long")
    return ", ".join(parts)


def render(data: dict, *, width: int = 100) -> str:
    """The screen as text. Every number and name comes straight from ``data``."""
    state, attention, gates = data["state"], data["attention"], data["gates"]
    lines: list[str] = []
    # A list counts as "known" only when the builder returned it and reported no error for that part.
    # Otherwise the screen says it could not read it; it never turns "unknown" into "none".
    agents_ok = isinstance(state.get("agents"), list) and not state.get("errors")
    threads_ok = isinstance(state.get("threads"), list) and not state.get("errors")
    items_ok = isinstance(attention.get("items"), list) and not attention.get("errors")
    agents = state["agents"] if isinstance(state.get("agents"), list) else []
    counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}

    lines.append(f"agenttalk overview - {_text(state.get('label', ''))}")
    summary = []
    if agents_ok:
        summary.append(_plural(len(agents), "agent", "agents"))
    if threads_ok and isinstance(counts.get("open_threads"), int):
        summary.append(_plural(counts["open_threads"], "open conversation", "open conversations"))
    needing = _attention_total(attention)
    if items_ok and needing is not None:
        summary.append(_plural(needing, "item needing attention", "items needing attention"))
    if isinstance(gates.get("verdict"), str) and not gates.get("errors"):
        count = gates.get("count")
        empty = gates["verdict"] == "GO" and type(count) is int and count == 0  # only a known zero (#610)
        verdict = "nothing checked (0 applicable gates)" if empty else _text(gates["verdict"])
        summary.append(f"release checks: {verdict}")
    if summary:
        lines.append(", ".join(summary))
    if items_ok and "operator_questions_pending" in attention:
        lines.append(f"{attention['operator_questions_pending']} operator question(s) pending")
    for source, what in ((state, "the team"), (attention, "attention items"), (gates, "release checks")):
        for err in source.get("errors") or []:
            lines.append(f"Could not read {what}: {_text(err)}")

    items = attention.get("items") if isinstance(attention.get("items"), list) else []
    lines += ["", "NEEDS ATTENTION"]
    if not items_ok and not items:
        lines.append("  Could not be read, so this may not be empty.")
    elif not items:
        lines.append("  Nothing right now.")
    for it in items[:MAX_ATTENTION]:
        who = f" ({_text(it['agent'])})" if it.get("agent") else ""
        age = "" if it.get("age_unknown") else f", {_age(it.get('age_seconds'))}"
        lines.append(f"  [{_text(it.get('severity', '?'))}] {_text(it.get('title', ''))}{who}{age}")
    if len(items) > MAX_ATTENTION:
        lines.append(f"  ... and {len(items) - MAX_ATTENTION} more (agenttalk attention)")

    threads = state.get("threads") if isinstance(state.get("threads"), list) else []
    lines += ["", "WHO IS WAITING FOR WHOM"]
    if not threads_ok and not threads:
        lines.append("  Could not be read, so this may not be empty.")
    elif not threads:
        lines.append("  No open conversations.")
    for th in threads[:MAX_THREADS]:
        subject = _text(th.get("subject") or th.get("request_id") or "(no subject)")
        owner = _owners(th.get("next_owner"))
        lines.append(f"  {subject}: next move is {owner}, open {_age(th.get('age_seconds'))}")
    if len(threads) > MAX_THREADS:
        lines.append(f"  ... and {len(threads) - MAX_THREADS} more (agenttalk threads)")

    lines += ["", "TEAM"]
    if not agents_ok and not agents:
        lines.append("  Could not be read, so this may not be empty.")
    elif not agents:
        lines.append("  No agents.")
    for a in agents:
        role = f" ({_text(a['role'])})" if a.get("role") else ""
        lines.append(f"  {_text(a.get('name', '?'))}{role}: {_seat_line(a)}")

    blocking = [g for g in gates.get("gates") or [] if isinstance(g, dict) and g.get("blocks") is True]
    if blocking:
        lines += ["", "RELEASE CHECKS HOLDING"]
        for g in blocking:
            reason = f": {_text(g['reason'])}" if g.get("reason") else ""
            lines.append(f"  {_text(g.get('name', '?'))}{reason}")

    if "budget" in data:
        lines += ["", "SPEND (this machine's paid-model ledger)"]
        lines += _budget_lines(data["budget"])

    lines += ["", "More detail: agenttalk dashboard (a web page), agenttalk threads, agenttalk attention."]
    return "\n".join(_wrap(_safe(line), width) for line in lines) + "\n"


def _budget_lines(budget: object) -> list[str]:
    if not isinstance(budget, dict):
        return ["  Not available."]
    status = budget.get("status")
    if status != "ok":
        words = {"not_set_up": "The paid gateway is not set up.",
                 "busy": "The ledger is busy; try again.",
                 "unavailable": "The figures could not be read."}
        return ["  " + words.get(status, "Not available.")]
    out = []
    for key, name in (("committed_micro_eur", "spent this month"),
                      ("trial_cutoff_micro_eur", "monthly limit"),
                      ("external_ceiling_micro_eur", "overall ceiling")):
        value = budget.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out.append(f"  {name}: {value / 1_000_000:.2f} EUR")
    if budget.get("service_hold"):
        out.append("  The gateway is on hold until an operator clears it.")
    if isinstance(budget.get("unresolved_attempts"), int) and budget["unresolved_attempts"]:
        out.append(f"  Unsettled calls: {budget['unresolved_attempts']}")
    return out or ["  Not available."]


def _wrap(line: str, width: int) -> str:
    """Wrap a long line under its own indent, so nothing is cut off."""
    if len(line) < width:
        return line
    indent = " " * (len(line) - len(line.lstrip()))
    return chr(10).join(textwrap.wrap(line, max(width - 1, 20), subsequent_indent=indent + "  ",
                                   break_long_words=True))


def terminal_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns
