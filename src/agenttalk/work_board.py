"""Pure work-board reducer (#207 slice B3a): validated envelopes in, work items out.

Correctness uses explicit correlation only: request_id/in_reply_to, supersedes
and declared cycle numbers. Message IDs and timestamps never order evidence.
Bodies and subjects are never read; seat names are compared as identities,
never parsed. Facts the bus cannot prove (operator incidents, integration,
fresh health, check results) are injected by later slices and default to unknown.
"""

import json

from agenttalk import work_tags
from agenttalk.threads import _classify_event

EXECUTION = {"design", "build", "fix"}
EARLIER_REQUESTER = "outstanding task from an earlier requester; current lead cannot cancel yet"
EXTERNAL = "external deliverable; authorship unverified"
_TAGS = ("work_item", "stage", "work_cycle", "work_round")


def _tag(meta, key, default=None):
    return work_tags.value(key, meta[key]) if key in meta else default


def _verdict(stage, meta):
    if meta.get("verdict_issue"):
        return None, meta["verdict_issue"]
    allowed = {"go": "GO", "fix": "FIX", "hold": "HOLD"} if stage in work_tags.REVIEWS else (
        {"done": "done"} if stage in EXECUTION else {})
    raw = meta.get("verdict")
    if raw is None:
        return None, "verdict missing"
    canonical = allowed.get(raw.casefold()) if isinstance(raw, str) else None
    return (canonical, None) if canonical else (None, "unrecognized verdict")


def _request(rid, copies, responses, running):
    first = copies[0]
    meta = first.meta
    req = {"request_id": rid, "kind": first.kind, "requester": first.sender, "issues": [], "obligations": []}
    if (any((c.sender, c.kind, c.meta) != (first.sender, first.kind, meta) for c in copies)
            or len({c.recipient for c in copies}) != len(copies)):
        req["issues"].append(("ambiguous fan-out openers", [c.id for c in copies]))
    try:
        tags = {key: _tag(meta, key) for key in _TAGS}
        head, supersedes = _tag(meta, "work_head"), _tag(meta, "supersedes")
        gates = meta.get("required_gates")
        gates = json.loads(gates) if isinstance(gates, str) else gates
        if gates is not None and not (isinstance(gates, list) and all(isinstance(g, str) for g in gates)):
            raise ValueError("required_gates must be a list of check keys")
    except ValueError:
        req["issues"].append(("malformed work metadata", [c.id for c in copies]))
        tags, head, supersedes, gates = {k: meta.get(k) for k in _TAGS}, None, None, None
    external = meta.get("external_deliverable")
    if external not in (None, True, False) and str(external).lower() not in ("true", "false"):
        req["issues"].append(("malformed work metadata", [c.id for c in copies]))
    req.update(work_item=meta.get("work_item") if isinstance(meta.get("work_item"), str) else None,
               stage=tags["stage"], cycle=int(tags["work_cycle"] or 1), explicit_cycle="work_cycle" in meta,
               round=tags["work_round"], head=head, supersedes=supersedes, title=meta.get("work_title"),
               external=external is True or str(external).lower() == "true",
               policy=(tuple(gates) if gates is not None else None, meta.get("no_gates_reason")))
    rescinds = [r for r in responses if r.kind == "rescind" and r.sender == first.sender]
    vendors = meta.get("assignee_model_vendors") if isinstance(meta.get("assignee_model_vendors"), dict) else {}
    for copy in copies:
        ob = {"request_id": rid, "opener": copy.id, "requester": copy.sender, "recipient": copy.recipient,
              "stage": req["stage"], "head": head, "state": "outstanding", "verdict": None, "issue": None,
              "accepted": False, "needs_info": False, "running": (copy.recipient, rid) in running,
              "reply": None, "vendor": vendors.get(copy.recipient, "unverified")}
        terminal = []
        for reply in responses:
            event = None if reply.kind == "rescind" else _classify_event(
                first.kind, reply, copy.sender, copy.recipient, copy.recipient)
            if event is None:
                continue
            if _contradicts(reply.meta, tags, head, req["stage"]):
                req["issues"].append(("reply contradicts its opener", [reply.id]))
            if event[0] == "terminal":
                terminal.append(reply)
            else:
                ob["accepted" if first.kind == "task" else "needs_info"] = True
        if rescinds and terminal:
            ob["state"] = "conflict"
            req["issues"].append(("reply and requester rescind both recorded; causal order unproven",
                                  [r.id for r in rescinds + terminal]))
        elif rescinds:
            ob.update(state="rescinded", reply=min(r.id for r in rescinds))
        elif len(terminal) > 1:
            ob["state"] = "conflict"
            req["issues"].append(("multiple terminal replies", [r.id for r in terminal]))
        elif terminal:
            reply = terminal[0]
            ob["reply"] = reply.id
            if reply.meta.get("status") == "declined":
                ob["state"] = "declined"
            else:
                ob["state"] = "done"
                ob["verdict"], ob["issue"] = _verdict(req["stage"], reply.meta)
        req["obligations"].append(ob)
    return req


def _contradicts(meta, tags, head, stage):
    try:
        if any(key in meta and _tag(meta, key) != (tags[key] or ("1" if key == "work_cycle" else None))
               for key in _TAGS):
            return True
        return stage in work_tags.REVIEWS and "work_head" in meta and _tag(meta, "work_head") != head
    except ValueError:
        return True


def reduce(messages, *, lead, incidents=(), integrated=None, running=frozenset(), checks=None):
    """Return {"items": [...], "legacy": {...}}; the input is never mutated or ordered by ID."""
    messages = list(messages)
    openers, opener_rid = {}, {}
    for m in messages:
        rid = m.meta.get("request_id")
        if m.kind in work_tags.OPENERS and isinstance(rid, str) and rid:
            openers.setdefault(rid, []).append(m)
            opener_rid[m.id] = rid
    responses, orphans = {}, {}
    for m in messages:
        if m.kind not in work_tags.REPLIES and m.kind != "rescind":
            continue
        rid, anchor = m.meta.get("request_id"), m.meta.get("in_reply_to")
        if anchor in opener_rid and rid not in (None, opener_rid[anchor]):
            why = "reply request_id contradicts its in_reply_to opener"
        else:
            rid = opener_rid.get(anchor, rid)
            if rid in openers:
                responses.setdefault(rid, []).append(m)
                continue
            why = "reply has no available opener"
        if isinstance(m.meta.get("work_item"), str) and m.kind != "rescind":
            orphans.setdefault(m.meta["work_item"], []).append((why, [m.id]))
    grouped, legacy = {}, []
    for rid, copies in openers.items():
        req = _request(rid, copies, responses.get(rid, []), running)
        (grouped.setdefault(req["work_item"], []) if req["work_item"] else legacy).append(req)
    facts = {"lead": lead, "incidents": list(incidents), "integrated": integrated or {}, "checks": checks or {}}
    items = [_item(slug, grouped.get(slug, []), orphans.get(slug, []), facts)
             for slug in sorted(set(grouped) | set(orphans))]
    return {"items": items, "legacy": _legacy(legacy)}


def _legacy(reqs):
    clean = [r for r in reqs if not r["issues"]]
    open_reqs = [r for r in clean if any(o["state"] == "outstanding" for o in r["obligations"])]
    examples = sorted(min(o["opener"] for o in r["obligations"]) for r in open_reqs)
    counts = {}
    for r in open_reqs:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    exact = len(clean) == len(reqs)
    return {"open_request_count": len(open_reqs) if exact else None, "known_lower_bound": len(open_reqs),
            "counts_by_kind": dict(sorted(counts.items())), "examples": examples[:20], "truncated": len(examples) > 20}


def _item(slug, reqs, orphans, facts):
    item = {"work_item": slug, "title": None, "cycle": None, "legacy_cycle": True, "round": None,
            "candidate": None, "builders": [], "verdicts": {}, "obligations": [], "incidents": [],
            "issues": [], "previous_cycles": []}
    if not reqs:
        return dict(item, column="unknown", workflow_column="unknown", row=2, reason=orphans[0][0],
                    evidence=sorted(i for _, ids in orphans for i in ids))
    titles = {r["title"] for r in reqs if r["title"]}
    item["title"] = titles.pop() if len(titles) == 1 else None
    if len(titles) > 1:
        item["issues"].append("conflicting explicit titles")
    current = max(r["cycle"] for r in reqs)
    cur = [r for r in reqs if r["cycle"] == current]
    obs = [o for r in cur for o in r["obligations"]]
    execs = [o for o in obs if o["stage"] in EXECUTION]
    builds_any = [o for r in reqs for o in r["obligations"] if o["stage"] in ("build", "fix")]
    design_only = not any(o["stage"] in ("build", "fix") for o in execs)
    builders = {o["recipient"] for o in builds_any}
    builders |= {o["recipient"] for o in execs if o["stage"] == "design"} if design_only else set()
    replaced = {}
    for r in cur:
        if r["supersedes"]:
            replaced.setdefault(r["supersedes"], []).extend(r["obligations"])
    external = [r for r in reqs if r["external"]]
    item.update(cycle=current, legacy_cycle=not any(r["explicit_cycle"] for r in reqs), builders=sorted(builders),
                round=max((int(r["round"]) for r in cur if r["round"]), default=None),
                obligations=sorted(({k: o[k] for k in ("request_id", "recipient", "stage", "state", "verdict")}
                                    for o in obs),
                                   key=lambda o: (o["request_id"], o["recipient"])))
    if external:
        item["issues"].append(EXTERNAL)
    for cycle in sorted({r["cycle"] for r in reqs} - {current}):
        old = [o for r in reqs if r["cycle"] == cycle for o in r["obligations"]]
        pending = sorted(o["request_id"] for o in old if o["state"] == "outstanding")
        note = ("design phase ended by lead; operator approval unrecorded"
                if not pending and all(o["stage"] == "design" for o in old)
                and any(r["requester"] == facts["lead"] for r in cur) else None)
        item["previous_cycles"].append({"cycle": cycle, "outstanding": pending, "note": note})
    item["incidents"] = sorted(i["id"] for i in facts["incidents"] if i.get("work_item") == slug
                               and str(i.get("work_cycle", "1")).lstrip("0") == str(current))
    row, column, reason, evidence = _place(slug, reqs, cur, obs, execs, builders, replaced, external,
                                           builds_any, orphans, facts, item)
    item.update(workflow_column=column, row=row, reason=reason, evidence=sorted(set(evidence)),
                column="needs_you" if item["incidents"] else column)
    return item


def _place(slug, reqs, cur, obs, execs, builders, replaced, external, builds_any, orphans, facts, item):
    conflicts = [issue for r in reqs for issue in r["issues"]] + orphans
    if external and builds_any:
        conflicts.append(("external deliverable conflicts with recorded build dispatches",
                          [o["opener"] for o in builds_any]))
    if conflicts:
        conflicts.sort(key=lambda c: (c[0], sorted(c[1])))
        return 2, "unknown", conflicts[0][0], [i for _, ids in conflicts for i in ids]
    reviews = [o for o in obs if o["stage"] in work_tags.REVIEWS]
    surviving = [o for o in reviews if o["request_id"] not in replaced and o["state"] != "rescinded"]
    heads = {o["head"] for o in surviving} | {r["head"] for r in external if r["cycle"] == item["cycle"]}
    candidate = next(iter(heads)) if len(heads) == 1 and None not in heads else None
    item["candidate"] = candidate
    for o in reviews:
        if o["state"] == "done" and o["verdict"]:
            item["verdicts"].setdefault(o["head"], []).append(
                {"reviewer": o["recipient"], "verdict": o["verdict"], "reply": o["reply"],
                 "independent": o["recipient"] not in builders, "vendor": o["vendor"]})
    for entries in item["verdicts"].values():
        entries.sort(key=lambda e: (e["reviewer"], e["reply"]))
    resolved = {rid for rid, reps in replaced.items() if any(o["verdict"] == "GO" for o in reps)}
    fixes = [o for o in reviews if o["verdict"] in ("FIX", "HOLD") and o["request_id"] not in resolved]
    open_fix = [o for o in fixes if o["request_id"] not in replaced]
    pending = [o for o in obs if o["state"] == "outstanding"]
    if candidate and facts["integrated"].get((slug, candidate)) and not any(o["state"] == "outstanding" for o in execs):
        reason = "merged with open FIX/HOLD" if fixes else "integrated in configured target"
        return 3, "done", reason, [o["reply"] for o in fixes] or [o["opener"] for o in surviving]
    active_fix = [o for o in execs if o["stage"] == "fix" and o["state"] == "outstanding"]
    if active_fix:
        state = "running" if any(o["running"] for o in active_fix) else (
            "accepted" if any(o["accepted"] for o in active_fix) else "dispatched")
        return 4, "fix_round", "fix " + state, [o["opener"] for o in active_fix]
    if any(o["verdict"] == "FIX" for o in open_fix):
        return 4, "fix_round", "unresolved FIX without replacement review", [
            o["reply"] for o in open_fix if o["verdict"] == "FIX"]
    building = [o for o in execs if o["state"] == "outstanding" and (o["accepted"] or o["running"])]
    if building:
        stage = "design" if all(o["stage"] == "design" for o in building) else "build"
        return 5, "building", stage + (" running" if any(o["running"] for o in building) else " accepted"), [
            o["opener"] for o in building]
    waiting = [o for o in reviews if o["state"] == "outstanding"]
    if waiting:
        reason = ("awaiting replacement review" if any(o["request_id"] in replaced for o in fixes)
                  else "independent review" if all(o["recipient"] not in builders for o in waiting)
                  else "unverified review")
        return 6, "independent_review", reason, [o["opener"] for o in waiting]
    blockers = _ready_blockers(slug, reqs, cur, execs, surviving, candidate, heads, builders, external, builds_any,
                               fixes, pending, facts, item)
    if not blockers:
        success = [o["reply"] for o in execs if o["verdict"] == "done"]
        gos = [o["reply"] for o in surviving if o["verdict"] == "GO"]
        return 7, "ready", "reviewed; independent GO; " + item["checks"], success + gos
    stalled = [o for o in execs if o["state"] == "outstanding"]
    if stalled:
        if facts["lead"] and any(o["requester"] != facts["lead"] for o in stalled):
            return 9, "unknown", EARLIER_REQUESTER, [o["opener"] for o in stalled if o["requester"] != facts["lead"]]
        return 8, "queued", "start unconfirmed", [o["opener"] for o in stalled]
    return 9, "unknown", blockers[0][0], blockers[0][1]


def _ready_blockers(slug, reqs, cur, execs, surviving, candidate, heads, builders, external, builds_any, fixes,
                    pending, facts, item):
    item["checks"] = None
    blockers = []
    if any(o["stage"] not in work_tags.STAGES for r in cur for o in r["obligations"]):
        blockers.append(("unknown stage", [o["opener"] for r in cur for o in r["obligations"]]))
    success = [o for o in execs if o["state"] == "done" and o["verdict"] == "done"]
    issues = sorted((o for o in execs + surviving if o["issue"]), key=lambda o: (o["issue"], o["reply"]))
    if issues:
        blockers.append((issues[0]["issue"], [o["reply"] for o in issues]))
    if execs and not success and all(o["state"] in ("declined", "rescinded") for o in execs):
        blockers.append(("all execution declined or cancelled", [o["opener"] for o in execs]))
    if not success and not (external and not builds_any):
        blockers.append(("no successful deliverable", [o["opener"] for o in execs]))
    if candidate is None:
        why = ("no review dispatched" if not heads else "candidate missing" if None in heads
               else "multiple candidates without supersession")
        blockers.append((why, [o["opener"] for o in surviving]))
    if pending:
        blockers.append(("pending execution or review", [o["opener"] for o in pending]))
    if any(o["verdict"] == "HOLD" for o in fixes):
        blockers.append(("non-operator HOLD", [o["reply"] for o in fixes if o["verdict"] == "HOLD"]))
    elif fixes:
        blockers.append(("unresolved FIX", [o["reply"] for o in fixes]))
    if any(o["state"] == "declined" for o in surviving):
        blockers.append(("declined review without replacement", [o["opener"] for o in surviving]))
    if not any(o["verdict"] == "GO" and o["head"] == candidate and o["recipient"] not in builders
               for o in surviving) or not all(o["verdict"] == "GO" for o in surviving):
        blockers.append(("no independent GO", [o["reply"] or o["opener"] for o in surviving]))
    policy = _policy(slug, cur, reqs, facts, item)
    if policy:
        blockers.append(policy)
    return blockers


def _policy(slug, cur, reqs, facts, item):
    # The originating dispatch defines policy; a fix-only later cycle inherits the earlier origin.
    origin = [r for r in reqs if not r["supersedes"] and (r["stage"] in ("design", "build") or r["external"])]
    sources = [r for r in origin if r in cur] or origin
    policies = {r["policy"] for r in sources if r["policy"] != (None, None)}
    evidence = [o["opener"] for r in sources for o in r["obligations"]]
    if not policies:
        return "check policy missing", evidence
    if len(policies) > 1:
        return "conflicting check policies", evidence
    gates, reason = policies.pop()
    if gates and reason:
        return "no_gates_reason conflicts with required gates", evidence
    if not gates:
        item["checks"] = "local checks not tracked" if reason else None
        return None if reason else ("check policy missing", evidence)
    result = facts["checks"].get((slug, item["cycle"]))
    if result is None:
        return "required check evidence unavailable", evidence
    item["checks"] = "required checks green"
    return None if result else ("required checks not satisfied", evidence)
