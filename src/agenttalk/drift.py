"""Drift visibility (#923, slice 1): advisory warnings and a read-only report.

Reads messages only. It never refuses, never changes delivery or an exit code, and keeps no
state of its own. Definitions D1-D4 follow the agreed design: a challenge reference must resolve
to a verdict stamped BEFORE the order; spent time is elapsed task-hours (a proxy that includes
waiting, not effort); a fix round is a distinct (work_cycle, work_round) pair counted once.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

STAGES = ("design", "build", "fix")
EXEMPT_IDS = ("failing-repro-fix", "review-ordered-fix", "checklist-release", "rollback", "revert",
              "docs-only", "operator-waiver", "incident-containment")
VERDICTS = ("proceed", "reshape", "probe", "replace", "defer", "stop")
DECISIONS = ("narrow", "stop", "continue")
FIX_ROUNDS = 2
# Orders sent before this UTC instant are "unknown" in the report (set to the release instant).
ACTIVATION = "2026-11-26T00:00:00Z"
_ALLOWANCE = re.compile(r"(\d+(?:\.\d+)?)h")
_BAD = ("pending", "unusable", "late", "unverifiable")


def _t(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _truthy(value):
    return value is True or str(value).lower() == "true"


def _by_rid(msgs):
    index = {}
    for m in msgs:
        rid = (m.meta or {}).get("request_id")
        if rid:
            index.setdefault(str(rid), []).append(m)
    return index


def ref_state(index, ref, order_ts, hop=0):
    """D1: the state of one challenge reference as of the order's timestamp."""
    found = index.get(ref, [])
    verdicts = [m for m in found if _truthy(m.meta.get("challenge")) and "verdict" in m.meta]
    if verdicts:
        good = [m for m in verdicts if str(m.meta["verdict"]) in VERDICTS]
        if not good:
            return "unusable"
        return "ok" if any((_t(m.ts) or order_ts) < order_ts for m in good) else "late"
    if any(_truthy(m.meta.get("challenge")) for m in found):
        return "pending"
    if hop == 0:  # reuse: a prior task is not proof; its own reference must resolve (one hop)
        for m in found:
            prior = str(m.meta.get("challenge") or "")
            if m.kind == "task" and prior and not prior.startswith("exempt:"):
                states = [ref_state(index, r.strip(), order_ts, 1) for r in prior.split(",") if r.strip()]
                if states and all(s == "ok" for s in states):
                    return "reused-ok"
    return "unverifiable"


def classify(index, meta, order_ts):
    """The class of one design/build/fix order, or 'unclassified' (no stage)."""
    if str(meta.get("stage") or "") not in STAGES:
        return "unclassified", []
    ref = str(meta.get("challenge") or "").strip()
    if not ref:
        return "absent", []
    if ref.startswith("exempt:"):
        return ("exempt-listed" if ref.split(":")[1] in EXEMPT_IDS else "exempt-unlisted"), []
    states = [ref_state(index, r.strip(), order_ts) for r in ref.split(",") if r.strip()]
    for bad in _BAD:
        if bad in states:
            return bad, states
    return ("reused-ok" if "reused-ok" in states else "ok"), states


def _allowance(tasks):
    """D4: the first valid allowance is the agreed one; later different values are attempts."""
    agreed, attempts = None, []
    for m in tasks:
        hit = _ALLOWANCE.fullmatch(str(m.meta.get("allowance") or ""))
        if hit and float(hit.group(1)) > 0:
            if agreed is None:
                agreed = float(hit.group(1))
            elif float(hit.group(1)) != agreed:
                attempts.append(hit.group(0))
    return agreed, attempts


def _spent(msgs, tasks, now):
    """D2: elapsed task-hours; ends at done/declined, supersession or rescind; open work runs to now."""
    first, hours, open_n, bad, index, sup = {}, 0.0, 0, 0, _by_rid(msgs), {}
    for m in tasks:
        first.setdefault((str(m.meta.get("request_id")), m.recipient), m)
        if m.meta.get("supersedes"):
            sup.setdefault(str(m.meta["supersedes"]), []).append(_t(m.ts))
    for (rid, who), m in first.items():
        start = _t(m.ts)
        if start is None:
            bad += 1
            continue
        ends = [_t(r.ts) for r in index.get(rid, []) if r.sender == who
                and ((r.kind == "task-response" and r.meta.get("status") in ("done", "declined"))
                     or (r.kind == "rescind" and r.sender == m.sender))]
        ends = [e for e in ends + sup.get(rid, []) if e is not None and e >= start]
        if ends:
            hours += (min(ends) - start).total_seconds() / 3600
        else:
            open_n += 1
            hours += max((now - start).total_seconds(), 0) / 3600
    return hours, open_n, bad


def _rounds(tasks):
    """D3: distinct (cycle, round) pairs of fix work, once each; untagged fix tasks counted apart."""
    pairs, untagged = set(), set()
    for m in tasks:
        rnd, stage = str(m.meta.get("work_round") or ""), m.meta.get("stage")
        if rnd and (stage == "fix" or int(rnd) > 1):
            pairs.add((str(m.meta.get("work_cycle") or "1"), rnd))
        elif stage == "fix":
            untagged.add(str(m.meta.get("request_id")))
    return len(pairs), len(untagged)


def work_item_state(msgs, work_item, now):
    tasks = sorted((m for m in msgs if m.kind == "task" and (m.meta or {}).get("work_item") == work_item),
                   key=lambda m: m.id)
    agreed, attempts = _allowance(tasks)
    hours, open_n, bad = _spent(msgs, tasks, now)
    k, u = _rounds(tasks)
    legacy = bool(tasks) and (_t(tasks[0].ts) or now) < _t(ACTIVATION)
    return {"work_item": work_item, "tasks": len(tasks), "allowance": agreed, "attempts": attempts,
            "hours": hours, "open": open_n, "incomplete": bad, "rounds": k, "untagged": u, "legacy": legacy}


def growth_line(s, decided):
    allow = f"{s['allowance']:g}h" if s["allowance"] else ("unknown (older work)" if s["legacy"] else "none usable")
    bound = "at least " if s["open"] or s["incomplete"] else ""
    rounds = f"{s['rounds']} tagged" + (f" + {s['untagged']} untagged (count incomplete)" if s["untagged"] else "")
    extra = f", {s['open']} open" if s["open"] else ""
    attempt = f"; attempted change to {', '.join(s['attempts'])} not applied" if s["attempts"] else ""
    return (f"work item {s['work_item']}: allowance {allow}, elapsed task-hours (a proxy, includes waiting) "
            f"{bound}{s['hours']:.1f}{extra}, fix rounds {rounds} of {FIX_ROUNDS}, "
            f"decision {'recorded' if decided else 'needed' if _over(s) else 'not needed'}{attempt}")


def _over(s):
    return s["rounds"] + s["untagged"] >= FIX_ROUNDS or bool(s["allowance"] and s["hours"] > s["allowance"])


def _decided(meta):
    return str(meta.get("decision")) in DECISIONS and bool(str(meta.get("decision_reason") or "").strip())


def order_warnings(msgs, meta, now=None):
    """Advisory lines for a task about to be sent (never a refusal)."""
    now = now or datetime.now(timezone.utc)
    out, stage = [], str(meta.get("stage") or "")
    if stage in STAGES:
        cls, states = classify(_by_rid(msgs), meta, now)
        if cls not in ("ok", "reused-ok", "exempt-listed"):
            out.append(f"challenge evidence for this {stage} order: {cls}"
                       + (f" ({', '.join(states)})" if states else "")
                       + " - no answered challenge verdict existed before this order")
    wi = meta.get("work_item")
    if not wi:
        if stage in STAGES:
            out.append("no work_item: this order's growth cannot be tracked")
        return out
    s = work_item_state(msgs, str(wi), now)
    new = _ALLOWANCE.fullmatch(str(meta.get("allowance") or ""))
    if s["tasks"] == 0 and stage in STAGES and not new:
        out.append(f"work item {wi}: no usable allowance (expected allowance=<N>h on the first order)")
    elif new and s["allowance"] and float(new.group(1)) != s["allowance"]:
        s["attempts"].append(new.group(0))
    if new and not s["allowance"] and not s["legacy"]:
        s["allowance"] = float(new.group(1))
    if s["tasks"] and not s["legacy"] or s["allowance"] or s["rounds"] + s["untagged"]:
        out.append(growth_line(s, _decided(meta)))
        if _over(s) and not _decided(meta):
            out.append(f"work item {wi}: two fix rounds or the allowance are used - this order needs "
                       "decision=narrow|stop|continue and decision_reason=<why>")
    return out


def report(msgs, since=None, now=None):
    """Read-only summary for `overview --drift`: class counts, warned orders and work items."""
    now = now or datetime.now(timezone.utc)
    first, act = _t(since) if since else None, _t(ACTIVATION)
    index, counts, warned, items = _by_rid(msgs), {}, [], set()
    for m in msgs:
        meta = m.meta or {}
        if m.kind != "task" or str(meta.get("stage") or "") not in STAGES:
            continue
        ts = _t(m.ts)
        if first and ts and ts < first:
            continue
        if ts is None or ts < act:
            cls = "unknown"
        else:
            cls, _ = classify(index, meta, ts)
            if cls not in ("ok", "reused-ok", "exempt-listed"):
                warned.append(f"{m.id} ({cls})")
        counts[cls] = counts.get(cls, 0) + 1
        if meta.get("work_item"):
            items.add(str(meta["work_item"]))
    known = sum(v for k, v in counts.items() if k != "unknown")
    return {"classes": counts, "nothing_share": (counts.get("absent", 0) / known) if known else None,
            "warned_orders": warned, "work_items": [work_item_state(msgs, w, now) for w in sorted(items)]}


def render_report(rep):
    lines = ["DRIFT (advisory; elapsed task-hours are a proxy, not effort)"]
    lines += [f"  {k}: {v}" for k, v in sorted(rep["classes"].items())] or ["  no design/build/fix orders"]
    share = rep["nothing_share"]
    lines.append("  nothing share (absent, of classifiable orders): " + ("n/a" if share is None else f"{share:.0%}"))
    lines.append("  Older records are unknown; a missing field is not proof that no challenge happened.")
    lines += ["  warned order: " + w for w in rep["warned_orders"]]
    lines += ["  " + growth_line(s, False) for s in rep["work_items"]]
    return "\n".join(lines) + "\n"
