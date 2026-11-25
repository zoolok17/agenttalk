"""Drift visibility (#923, slice 1): advisory warnings and a read-only report.

Reads messages only. It never refuses, never changes delivery or an exit code, and keeps no
state of its own. Definitions D1-D4 follow the agreed design: a challenge reference must resolve
to a verdict stamped BEFORE the order; spent time is elapsed task-hours (a proxy that includes
waiting, not effort); a fix round is a distinct (work_cycle, work_round) pair counted once.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

STAGES = ("design", "build", "fix")
EXEMPT_IDS = ("failing-repro-fix", "review-ordered-fix", "checklist-release", "rollback", "revert",
              "docs-only", "operator-waiver", "incident-containment")
VERDICTS = ("proceed", "reshape", "probe", "replace", "defer", "stop")
DECISIONS = ("narrow", "stop", "continue")
FIX_ROUNDS = 2
# UNSET until the release checklist fills it with the release's UTC instant. While it is None the
# report calls every order unknown and no work item is treated as older work: nothing becomes a
# historical boundary by accident.
ACTIVATION: str | None = None
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)h")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_BAD = ("pending", "unusable", "late", "unverifiable")
_GOOD = ("ok", "reused-ok", "exempt-listed")


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


def _amount(raw):
    hit = _AMOUNT.fullmatch(str(raw or ""))
    return float(hit.group(1)) if hit and float(hit.group(1)) > 0 else None


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
    if hop == 0:  # reuse: the prior task must predate this order and its own reference must resolve (one hop)
        for m in found:
            prior = str(m.meta.get("challenge") or "")
            if m.kind == "task" and prior and not prior.startswith("exempt:") and (_t(m.ts) or order_ts) < order_ts:
                states = [ref_state(index, r.strip(), order_ts, 1) for r in prior.split(",")]
                if all(s == "ok" for s in states):
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
    parts = [r.strip() for r in ref.split(",")]
    if not all(_ID.fullmatch(r) for r in parts):  # empty or malformed segments are never evidence
        return "unusable", []
    states = [ref_state(index, r, order_ts) for r in parts]
    for bad in _BAD:
        if bad in states:
            return bad, states
    return ("reused-ok" if "reused-ok" in states else "ok"), states


def _spent(msgs, tasks, now):
    """D2: elapsed task-hours; ends at done/declined, the requester's rescind or a supersession."""
    first, hours, open_n, bad, index, sup = {}, 0.0, 0, 0, _by_rid(msgs), {}
    for m in tasks:
        first.setdefault((str(m.meta.get("request_id")), m.recipient), m)
    for m in msgs:  # a task OR a review-request may replace a task; only tasks are charged
        if m.kind in ("task", "review-request") and (m.meta or {}).get("supersedes"):
            sup.setdefault(str(m.meta["supersedes"]), []).append(_t(m.ts))
    for (rid, who), m in first.items():
        start, ends = _t(m.ts), []
        if start is None:
            bad += 1
            continue
        for r in index.get(rid, []):
            if (r.kind == "task-response" and r.sender == who and r.meta.get("status") in ("done", "declined")) \
                    or (r.kind == "rescind" and r.sender == m.sender):
                ends.append(_t(r.ts))
        ends = [e for e in ends + sup.get(rid, []) if e is not None and e >= start]
        if ends:
            hours += (min(ends) - start).total_seconds() / 3600
        else:
            open_n += 1
            hours += max((now - start).total_seconds(), 0) / 3600
    return hours, open_n, bad, len({m.recipient for m in first.values()}) > 1


def _rounds(tasks):
    """D3: distinct (cycle, round) pairs of fix work, once each; untagged fix tasks counted apart."""
    pairs, untagged = set(), set()
    for m in tasks:
        rnd, stage = str(m.meta.get("work_round") or ""), m.meta.get("stage")
        rnd = rnd if re.fullmatch(r"[0-9]+", rnd) else ""  # a malformed historical number is untagged, never a crash
        if rnd and (stage == "fix" or int(rnd) > 1):
            pairs.add((str(m.meta.get("work_cycle") or "1"), rnd))
        elif stage == "fix":
            untagged.add(str(m.meta.get("request_id")))
    return len(pairs), len(untagged)


def work_item_state(msgs, work_item, now):
    """D2-D4 for one work item as of `now`; `msgs` must already be only what existed then."""
    tasks = sorted((m for m in msgs if m.kind == "task" and (m.meta or {}).get("work_item") == work_item),
                   key=lambda m: m.id)
    act = _t(ACTIVATION) if ACTIVATION else None
    legacy = bool(tasks and act and (_t(tasks[0].ts) or now) < act)
    agreed, attempts = None, []
    for m in tasks:
        amount = _amount(m.meta.get("allowance"))
        if amount and agreed is None:
            agreed = amount
        elif amount and amount != agreed:
            attempts.append(f"{amount:g}h")
    hours, open_n, bad, parallel = _spent(msgs, tasks, now)
    k, u = _rounds(tasks)
    last = tasks[-1].meta if tasks else {}
    return {"work_item": work_item, "tasks": len(tasks), "decided": _decided(last),
            "allowance": None if legacy else agreed,
            "attempts": attempts, "hours": hours, "open": open_n, "incomplete": bad, "parallel": parallel,
            "rounds": k, "untagged": u, "legacy": legacy,
            "scoped": any((m.meta.get("stage") in STAGES) for m in tasks)}


def _decided(meta):
    return str(meta.get("decision")) in DECISIONS and bool(str(meta.get("decision_reason") or "").strip())


def _over(s):
    """The threshold is proven only by tagged rounds (a lower bound) or by elapsed time past the allowance."""
    return s["rounds"] >= FIX_ROUNDS or bool(s["allowance"] and s["hours"] > s["allowance"])


def growth_line(s, decided):
    allow = f"{s['allowance']:g}h" if s["allowance"] else ("unknown (older work)" if s["legacy"] else "none usable")
    bound = "at least " if s["open"] or s["incomplete"] else ""
    notes = (f", {s['open']} open" if s["open"] else "") + (
        f", incomplete history, {s['incomplete']} tasks" if s["incomplete"] else "") + (
        ", recipients added" if s["parallel"] else "")
    rounds = f"{s['rounds']} tagged" + (f" + {s['untagged']} untagged (count incomplete)" if s["untagged"] else "")
    need = "recorded" if decided else "needed" if _over(s) else (
        "possibly needed" if s["rounds"] + s["untagged"] >= FIX_ROUNDS else "not needed")
    attempt = f"; attempted change to {', '.join(s['attempts'])} not applied" if s["attempts"] else ""
    return (f"work item {s['work_item']}: allowance {allow}, elapsed task-hours (a proxy, includes waiting) "
            f"{bound}{s['hours']:.1f}{notes}, fix rounds {rounds} of {FIX_ROUNDS}, decision {need}{attempt}")


def check_order(msgs, meta, now, index=None, complete=True):
    """(warnings, growth line or None) for one order, given only the messages that existed before it."""
    out, stage, wi = [], str(meta.get("stage") or ""), meta.get("work_item")
    if stage in STAGES:
        cls, states = classify(index or _by_rid(msgs), meta, now)
        if cls not in _GOOD:
            seen = f" ({', '.join(states)})" if states else ""
            out.append(f"challenge evidence for this {stage} order: {cls}{seen}"
                       + " - no answered challenge verdict existed before this order")
    if not wi:
        return out + (["no work_item: this order's growth cannot be tracked"] if stage in STAGES else []), None
    s = work_item_state(msgs, str(wi), now)
    new_amount, first_scoped = _amount(meta.get("allowance")), stage in STAGES and not s["scoped"]
    if first_scoped and not new_amount and complete:
        out.append(f"work item {wi}: no usable allowance (expected allowance=<N>h, positive, on the first order)")
    if new_amount and not s["legacy"]:
        if s["allowance"] and new_amount != s["allowance"]:
            s["attempts"].append(f"{new_amount:g}h")
        s["allowance"] = s["allowance"] or new_amount
    decided = _decided(meta)
    if _over(s) and not decided:
        out.append(f"work item {wi}: two fix rounds or the allowance are used - this order needs "
                   "decision=narrow|stop|continue and decision_reason=<why>")
    show = s["allowance"] or first_scoped or s["rounds"] + s["untagged"]
    return out, (growth_line(s, decided) if show else None)


def read_messages(store, ceiling=3.0, clock=time.monotonic):
    """(messages, complete): newest files first, in chunks, until the time ceiling is passed."""
    if store.signing_enforced():  # the trust gate cannot be chunked: use it whole
        return store.valid_messages(), True
    names, got, start = sorted(store.messages_dir.glob("*.json"), reverse=True), [], clock()
    roster = set(store.load_config().get("agents") or [])
    for i in range(0, len(names), 500):
        if clock() - start > ceiling:
            return sorted(got, key=lambda m: m.id), False
        got += [m for m, _ in store._scan_messages_with_paths(paths=names[i:i + 500])[0] if m.sender in roster]
    return sorted(got, key=lambda m: m.id), True


def order_warnings(msgs, meta, now=None, complete=True):
    """Advisory lines for a task about to be sent (never a refusal)."""
    out, growth = check_order(msgs, meta, now or datetime.now(timezone.utc), complete=complete)
    note = [] if complete else ["history incomplete (stopped at the ceiling): allowance and rounds are lower bounds"]
    return out + note + ([growth] if growth else [])


def report(msgs, since=None, now=None):
    """Read-only summary for `overview --drift`. Warnings are RECONSTRUCTED as of each order's own
    time from the messages; they do not prove that stderr was shown."""
    now, msgs = now or datetime.now(timezone.utc), sorted(msgs, key=lambda m: m.id)
    act, first = (_t(ACTIVATION) if ACTIVATION else None), (_t(since) if since else None)
    index, counts, warned, items = _by_rid(msgs), {}, [], set()
    for i, m in enumerate(msgs):
        meta, ts = m.meta or {}, _t(m.ts)
        if m.kind != "task" or ts is None or (first and ts < first):
            continue
        if meta.get("work_item"):
            items.add(str(meta["work_item"]))
        staged = str(meta.get("stage") or "") in STAGES
        known = act is not None and ts >= act
        cls = classify(index, meta, ts)[0] if known and staged else ("unknown" if staged else "unclassified")
        if staged or cls == "unclassified":
            counts[cls] = counts.get(cls, 0) + 1
        if known and (staged or meta.get("work_item")):
            lines, _ = check_order(msgs[:i], meta, ts, index)
            if lines:
                warned.append({"order": m.id, "class": cls, "reasons": lines, "decision": meta.get("decision"),
                               "decision_reason": meta.get("decision_reason"), "reconstructed": True})
    rated = sum(v for k, v in counts.items() if k not in ("unknown", "unclassified", "exempt-listed"))
    return {"activation": ACTIVATION, "classes": counts,
            "nothing_share": (counts.get("absent", 0) / rated) if rated else None,
            "warned_orders": warned, "work_items": [work_item_state(msgs, w, now) for w in sorted(items)]}


def render_report(rep):
    lines = ["DRIFT (advisory; elapsed task-hours are a proxy, not effort)"]
    if rep["activation"] is None:
        lines.append("  Activation instant is not set (release checklist): every order is shown as unknown.")
    lines += [f"  {k}: {v}" for k, v in sorted(rep["classes"].items())] or ["  no design/build/fix orders"]
    share = rep["nothing_share"]
    lines.append("  nothing share (absent, of rated orders; exemptions, unknown and unclassified excluded): "
                 + ("n/a" if share is None else f"{share:.0%}"))
    lines.append("  Older records are unknown; a missing field is not proof that no challenge happened.")
    for w in rep["warned_orders"]:
        lines.append(f"  warned order {w['order']} (reconstructed from messages, not proof it was shown): "
                     + "; ".join(w["reasons"]) + (f" [decision {w['decision']}]" if w["decision"] else ""))
    lines += ["  " + growth_line(s, s["decided"]) for s in rep["work_items"]]
    return "\n".join(lines) + "\n"
