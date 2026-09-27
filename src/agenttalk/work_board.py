"""Pure work-board reducer (#207 slices B3a/B3b): validated envelopes in, work items out.

Correctness uses explicit correlation only: request_id/in_reply_to, supersedes
and declared cycle numbers. Message IDs and timestamps never order evidence.
Bodies and subjects are never read; seat names are compared as identities,
never parsed. Facts the bus cannot prove (operator incidents, integration,
fresh health, check results) are injected by later slices and default to unknown.
Malformed or partial history degrades only the affected item to Unknown with
evidence; it never aborts the reduction.
"""

import json

from agenttalk import work_tags
from agenttalk.threads import _classify_event

EXECUTION = {"design", "build", "fix"}
EARLIER_REQUESTER = "outstanding task from an earlier requester; current lead cannot cancel yet"
EXTERNAL = "external deliverable; authorship unverified"
_TAGS = ("work_item", "stage", "work_cycle", "work_round", "external_deliverable")
_DEFAULTS = {"work_cycle": "1", "external_deliverable": False}
_POLICY = ("work_repo", "work_branch", "work_target", "no_gates_reason")


def _tag(meta, key):
    return work_tags.value(key, meta[key]) if key in meta else None


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


def _parse(meta):
    """Normalized work fields of one opener; ValueError/TypeError marks malformed history."""
    tags = {key: _tag(meta, key) for key in _TAGS}
    gates = meta.get("required_gates")
    gates = json.loads(gates) if isinstance(gates, str) else gates
    if gates is not None and not (isinstance(gates, list) and all(isinstance(g, str) for g in gates)):
        raise ValueError("required_gates must be a list of check keys")
    declared = [meta.get(key) for key in _POLICY]
    if any(v is not None and not isinstance(v, str) for v in declared):
        raise ValueError("repository/check policy must be text")
    # The whole repository/check declaration is compared, not just its check half.
    policy = (*declared[:3], tuple(gates) if gates is not None else None, declared[3])
    vendors = meta.get("assignee_model_vendors")
    if vendors is not None and not (isinstance(vendors, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in vendors.items())):
        raise ValueError("assignee_model_vendors must map recipients to vendors")
    title = meta.get("work_title")
    return tags, {"stage": tags["stage"], "cycle": int(tags["work_cycle"] or 1),
                  "title": None if title is None else work_tags.value("work_title", title), "vendors": vendors,
                  "round": int(tags["work_round"]) if tags["work_round"] else None,
                  "head": _tag(meta, "work_head"), "supersedes": _tag(meta, "supersedes"),
                  "external": tags["external_deliverable"] is True,
                  "policy": policy if any(v is not None for v in policy) else None}


def _request(rid, copies, responses, running, descends):
    first, meta = copies[0], copies[0].meta
    openers = sorted(c.id for c in copies)
    req = {"request_id": rid, "kind": first.kind, "requester": first.sender, "openers": openers,
           "issues": [], "obligations": [], "title": None, "vendors": None, "explicit_cycle": "work_cycle" in meta,
           "work_item": meta.get("work_item") if isinstance(meta.get("work_item"), str) else None,
           "stage": None, "cycle": None, "round": None, "head": None, "supersedes": None,
           "external": False, "policy": None}
    if (any((c.sender, c.kind, c.meta) != (first.sender, first.kind, meta) for c in copies)
            or len({c.recipient for c in copies}) != len(copies)):
        req["issues"].append(("ambiguous fan-out openers", openers))
    try:
        tags, parsed = _parse(meta)
    except (TypeError, ValueError):
        req["issues"].append(("malformed work metadata", openers))
        tags = None
    else:
        req.update(parsed)
    vendors = req["vendors"] or {}
    # N3: the publisher's frozen map names every recipient; a missing copy is an incomplete snapshot.
    if req["vendors"] is not None and set(vendors) - {c.recipient for c in copies}:
        req["issues"].append(("incomplete fan-out: frozen recipient map names a missing opener", openers))
    if req["vendors"] is not None and {c.recipient for c in copies} - set(vendors):
        req["issues"].append(("fan-out copy outside its frozen recipient map", openers))
    rescinds = [r for r in responses if r.kind == "rescind" and r.sender == first.sender]
    for copy in copies:
        ob = {"request_id": rid, "opener": copy.id, "requester": copy.sender, "recipient": copy.recipient,
              "stage": req["stage"], "cycle": req["cycle"], "head": req["head"], "state": "outstanding",
              "verdict": None, "issue": None, "accepted": False, "holds": [], "hold_open": False,
              "running": (copy.recipient, rid) in running, "reply": None,
              "vendor": vendors.get(copy.recipient, "unverified")}
        terminal = []
        for reply in responses:
            event = None if reply.kind == "rescind" else _classify_event(
                first.kind, reply, copy.sender, copy.recipient, copy.recipient)
            if event is None:
                continue
            if tags is not None and _contradicts(reply.meta, tags, req["head"], req["stage"]):
                req["issues"].append(("reply contradicts its opener", [reply.id]))
            if event[0] == "terminal":
                terminal.append(reply)
            elif first.kind == "task":
                ob["accepted"] = True
            else:
                ob["holds"].append(reply.id)  # native needs-info is a HOLD (design section 2)
        ob["holds"].sort()
        ob["hold_open"] = bool(ob["holds"])  # a rescind or silence never answers a HOLD
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
            # N1: publication does not order a result and a needs-info; only explicit reply links do.
            before = [h for h in ob["holds"] if descends(reply.id, h)]
            after = [h for h in ob["holds"] if descends(h, reply.id)]
            if set(before) | set(after) != set(ob["holds"]):
                req["issues"].append(("ambiguous response order", [reply.id, *ob["holds"]]))
            ob["hold_open"] = bool(after)
        req["obligations"].append(ob)
    return req


def _contradicts(meta, tags, head, stage):
    try:
        if any(key in meta and _tag(meta, key) != (_DEFAULTS.get(key) if tags[key] is None else tags[key])
               for key in _TAGS):
            return True
        return stage in work_tags.REVIEWS and "work_head" in meta and _tag(meta, "work_head") != head
    except (TypeError, ValueError):
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
    parent = {m.id: m.meta.get("in_reply_to") for m in messages if isinstance(m.meta.get("in_reply_to"), str)}

    def descends(later, earlier):
        seen, node = set(), parent.get(later)
        while node is not None and node not in seen:
            if node == earlier:
                return True
            seen.add(node)
            node = parent.get(node)
        return False

    grouped, legacy = {}, []
    for rid, copies in openers.items():
        req = _request(rid, copies, responses.get(rid, []), running, descends)
        (grouped.setdefault(req["work_item"], []) if req["work_item"] else legacy).append(req)
    facts = {"lead": lead, "incidents": list(incidents), "integrated": integrated or {}, "checks": checks or {},
             "known": set(openers)}
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


def _graph(reqs, known):
    """Explicit replacement edges; a missing, foreign, branching or cyclic edge is a history conflict."""
    by_rid = {r["request_id"]: r for r in reqs}
    successor, conflicts = {}, []
    for r in reqs:
        target = r["supersedes"]
        old = by_rid.get(target)
        if target is None:
            continue
        if old is None:
            why = "supersedes crosses work items" if target in known else "missing referenced opener"
            conflicts.append((why, r["openers"]))
        elif old["cycle"] != r["cycle"]:
            conflicts.append(("supersedes crosses work cycles", old["openers"] + r["openers"]))
        elif target in successor:
            conflicts.append(("ambiguous branching replacements", successor[target]["openers"] + r["openers"]))
        else:
            successor[target] = r
    for r in reqs:
        seen, node = {r["request_id"]}, successor.get(r["request_id"])
        while node is not None:
            if node["request_id"] in seen:
                conflicts.append(("supersession cycle", sorted(i for rid in seen for i in by_rid[rid]["openers"])))
                break
            seen.add(node["request_id"])
            node = successor.get(node["request_id"])
    return by_rid, successor, conflicts


def _item(slug, reqs, orphans, facts):
    item = {"work_item": slug, "title": None, "cycle": None, "legacy_cycle": True, "round": None,
            "candidate": None, "builders": [], "verdicts": {}, "obligations": [], "incidents": [],
            "issues": [], "previous_cycles": [], "checks": None,
            "integration": dict(sorted((head, value) for (item_slug, head), value in facts["integrated"].items()
                                       if item_slug == slug))}
    if not reqs:
        return dict(item, column="unknown", workflow_column="unknown", row=2, reason=orphans[0][0],
                    evidence=sorted(i for _, ids in orphans for i in ids))
    titles = {r["title"] for r in reqs if r["title"]}
    item["title"] = titles.pop() if len(titles) == 1 else None
    if len(titles) > 1:
        item["issues"].append("conflicting explicit titles")
    valid = [r for r in reqs if r["cycle"] is not None]
    current = max((r["cycle"] for r in valid), default=None)
    cur = [r for r in valid if r["cycle"] == current]
    by_rid, successor, conflicts = _graph(valid, facts["known"])
    conflicts += [issue for r in reqs for issue in r["issues"]] + list(orphans)
    # M3 (lead ruling): an external declaration conflicts only with a build/fix of the SAME cycle.
    external_cycles = {r["cycle"] for r in valid if r["external"]}
    mixed = [r for r in valid if r["cycle"] in external_cycles and (r["external"] or r["stage"] in ("build", "fix"))]
    if any(r["stage"] in ("build", "fix") for r in mixed):
        conflicts.append(("external deliverable conflicts with recorded build dispatches",
                          [i for r in mixed for i in r["openers"]]))
    # Incomparable heads are a history conflict before any activity row (section 3).
    surviving = [r for r in cur if r["stage"] in work_tags.REVIEWS and r["request_id"] not in successor
                 and any(o["state"] != "rescinded" for o in r["obligations"])]
    if len({r["head"] for r in surviving} - {None}) > 1:
        conflicts.append(("multiple candidates without supersession", [i for r in surviving for i in r["openers"]]))
    build_purpose = any(r["stage"] == "build" for r in cur)
    builders = {o["recipient"] for r in valid if r["stage"] in ("build", "fix") for o in r["obligations"]}
    if not build_purpose:  # design purpose survives explicit fix ancestry; conservatively keep its authors
        builders |= {o["recipient"] for r in cur if r["stage"] == "design" for o in r["obligations"]}
    item["builders"] = sorted(builders)
    for r in cur:
        for o in r["obligations"] if r["stage"] in work_tags.REVIEWS else ():
            final = [(o["verdict"], o["reply"])] if o["state"] == "done" and o["verdict"] else []
            for verdict, reply in final + [("HOLD", h) for h in o["holds"]]:  # every HOLD stays as evidence
                item["verdicts"].setdefault(o["head"], []).append(
                    {"reviewer": o["recipient"], "verdict": verdict, "reply": reply,
                     "independent": o["recipient"] not in builders, "vendor": o["vendor"]})
    for entries in item["verdicts"].values():
        entries.sort(key=lambda e: (e["reviewer"], e["reply"]))
    policy = _policy(slug, valid, cur, current, facts) if current is not None else {
        "conflict": None, "problem": ("check policy missing", []), "label": None}
    item["checks"] = policy["label"]
    if policy["conflict"]:
        conflicts.append(policy["conflict"])
    obs = [o for r in cur for o in r["obligations"]]
    item.update(cycle=current, legacy_cycle=not any(r["explicit_cycle"] for r in reqs),
                round=max((r["round"] for r in cur if r["round"]), default=None),
                obligations=sorted(({k: o[k] for k in ("request_id", "recipient", "stage", "state", "verdict")}
                                    for o in obs), key=lambda o: (o["request_id"], o["recipient"])))
    if any(r["external"] for r in valid):
        item["issues"].append(EXTERNAL)
    for cycle in sorted({r["cycle"] for r in valid} - {current}):
        old = [o for r in valid if r["cycle"] == cycle for o in r["obligations"]]
        pending = sorted(o["request_id"] for o in old if o["state"] == "outstanding")
        note = ("design phase ended by lead; operator approval unrecorded"
                if not pending and all(o["stage"] == "design" for o in old)
                and any(r["requester"] == facts["lead"] for r in cur) else None)
        item["previous_cycles"].append({"cycle": cycle, "outstanding": pending, "note": note})
    item["incidents"] = sorted(i["id"] for i in facts["incidents"] if i.get("work_item") == slug
                               and str(i.get("work_cycle", "1")).lstrip("0") == str(current))
    if conflicts:
        conflicts.sort(key=lambda c: (c[0], sorted(c[1])))
        row, column, reason, evidence = 2, "unknown", conflicts[0][0], [i for _, ids in conflicts for i in ids]
    else:
        row, column, reason, evidence = _place(slug, valid, cur, by_rid, successor, surviving, policy, facts, item)
    item.update(workflow_column=column, row=row, reason=reason, evidence=sorted(set(evidence)),
                column="needs_you" if item["incidents"] else column)
    return item


def _live(r):
    return [o for o in r["obligations"] if o["state"] != "rescinded"]


def _place(slug, valid, cur, by_rid, successor, surviving, policy, facts, item):
    def head_of(r):
        seen = set()
        while r["request_id"] in successor and r["request_id"] not in seen:
            seen.add(r["request_id"])
            r = successor[r["request_id"]]
        return r

    def descends_from_design(r):
        seen = set()
        while r["supersedes"] in by_rid and r["request_id"] not in seen:
            seen.add(r["request_id"])
            r = by_rid[r["supersedes"]]
            if r["stage"] == "design":
                return True
        return False

    obs = [o for r in cur for o in r["obligations"]]
    execs = [o for o in obs if o["stage"] in EXECUTION]
    reviews = [r for r in cur if r["stage"] in work_tags.REVIEWS]
    heads = {r["head"] for r in surviving}
    candidate = next(iter(heads)) if len(heads) == 1 and None not in heads else None
    builders = set(item["builders"])
    item["candidate"] = candidate
    unresolved, awaiting = [], []
    for r in reviews:
        for o in r["obligations"]:
            final = [(o["verdict"], o["reply"])] if o["state"] == "done" and o["verdict"] else []
            marks = [m for m in final if m[0] in ("FIX", "HOLD")] + (
                [("HOLD", h) for h in o["holds"]] if o["hold_open"] else [])
            for verdict, reply in marks:
                head = head_of(r)
                live = _live(head)
                if head is not r and live and all(x["state"] == "done" and x["verdict"] == "GO" for x in live):
                    continue  # discharged by the surviving end of an explicit replacement chain
                pending = head is not r and any(x["state"] == "outstanding" for x in head["obligations"])
                (awaiting if pending else unresolved).append((verdict, reply))
    open_marks = unresolved + awaiting
    if candidate and facts["integrated"].get((slug, candidate)) and not any(
            o["state"] == "outstanding" for o in execs):
        independent = any(o["verdict"] == "GO" and o["head"] == candidate and o["recipient"] not in builders
                          for r in surviving for o in _live(r))
        # Missing review or check evidence stays explicit on Done.
        problem = policy["problem"]
        reason = ("merged with open FIX/HOLD" if open_marks else "integrated without independent GO" if not independent
                  else "integrated with failed required checks" if item["checks"] == "required checks failed"
                  else "integrated; " + problem[0] if problem else "integrated in configured target")
        return 3, "done", reason, [reply for _, reply in open_marks] or [i for r in surviving for i in r["openers"]]
    active_fix = [o for o in execs if o["stage"] == "fix" and o["state"] == "outstanding"]
    if active_fix:
        state = "running" if any(o["running"] for o in active_fix) else (
            "accepted" if any(o["accepted"] for o in active_fix) else "dispatched")
        return 4, "fix_round", "fix " + state, [o["opener"] for o in active_fix]
    if any(verdict == "FIX" for verdict, _ in unresolved):
        return 4, "fix_round", "unresolved FIX without replacement review", [
            reply for verdict, reply in unresolved if verdict == "FIX"]
    building = [o for o in execs if o["state"] == "outstanding" and (o["accepted"] or o["running"])]
    if building:
        stage = "design" if all(o["stage"] == "design" for o in building) else "build"
        return 5, "building", stage + (" running" if any(o["running"] for o in building) else " accepted"), [
            o["opener"] for o in building]
    waiting = [o for r in reviews for o in r["obligations"] if o["state"] == "outstanding"]
    if waiting:
        reason = ("awaiting replacement review" if awaiting
                  else "independent review" if all(o["recipient"] not in builders for o in waiting)
                  else "unverified review")
        return 6, "independent_review", reason, [o["opener"] for o in waiting]
    blockers = _blockers(cur, obs, execs, surviving, successor, candidate, heads, builders,
                         open_marks, descends_from_design, policy)
    if not blockers:
        success = [o["reply"] for o in execs if o["verdict"] == "done"]
        gos = [o["reply"] for r in surviving for o in _live(r) if o["verdict"] == "GO"]
        return 7, "ready", "reviewed; independent GO; " + item["checks"], success + gos
    stalled = [o for o in execs if o["state"] == "outstanding"]
    if stalled:
        if facts["lead"] and any(o["requester"] != facts["lead"] for o in stalled):
            return 9, "unknown", EARLIER_REQUESTER, [o["opener"] for o in stalled if o["requester"] != facts["lead"]]
        return 8, "queued", "start unconfirmed", [o["opener"] for o in stalled]
    return 9, "unknown", blockers[0][0], blockers[0][1]


def _blockers(cur, obs, execs, surviving, successor, candidate, heads, builders, open_marks,
              descends_from_design, policy):
    blockers = []
    if any(o["stage"] not in work_tags.STAGES for o in obs):
        blockers.append(("unknown stage", [o["opener"] for o in obs]))
    # Only the surviving end of each explicit chain can satisfy an execution obligation.
    current_exec = [o for r in cur if r["stage"] in EXECUTION and r["request_id"] not in successor for o in _live(r)]
    current_reviews = [o for r in surviving for o in _live(r)]
    issues = sorted((o for o in current_exec + current_reviews if o["issue"]), key=lambda o: (o["issue"], o["reply"]))
    if issues:
        blockers.append((issues[0]["issue"], [o["reply"] for o in issues]))
    success = [o for o in current_exec if o["state"] == "done" and o["verdict"] == "done"]
    if execs and all(o["state"] in ("declined", "rescinded") for o in execs):
        blockers.append(("all execution declined or cancelled", [o["opener"] for o in execs]))
    elif not success and not any(r["external"] for r in cur):
        blockers.append(("no successful surviving deliverable", [o["opener"] for o in execs]))
    if any(o["state"] == "declined" for o in current_exec):
        blockers.append(("declined execution without replacement",
                         [o["opener"] for o in current_exec if o["state"] == "declined"]))
    if candidate is None:
        why = ("no review dispatched" if not heads else "candidate missing" if None in heads
               else "multiple candidates without supersession")
        blockers.append((why, [i for r in surviving for i in r["openers"]]))
    pending = [o for o in obs if o["state"] == "outstanding"]
    if pending:
        blockers.append(("pending execution or review", [o["opener"] for o in pending]))
    if open_marks:
        blockers.append(("non-operator HOLD" if any(v == "HOLD" for v, _ in open_marks) else "unresolved FIX",
                         [reply for _, reply in open_marks]))
    if any(o["state"] == "declined" for o in current_reviews):
        blockers.append(("declined review without replacement", [o["opener"] for o in current_reviews]))
    if not all(o["state"] == "done" and o["verdict"] == "GO" for o in current_reviews) or not any(
            o["verdict"] == "GO" and o["head"] == candidate and o["recipient"] not in builders
            for o in current_reviews):
        blockers.append(("no independent GO", [o["reply"] or o["opener"] for o in current_reviews]))
    designs = any(r["stage"] == "design" for r in cur) and not any(r["stage"] == "build" for r in cur)
    unlinked = [r for r in cur if designs and r["stage"] == "fix" and not descends_from_design(r)]
    if unlinked:
        blockers.append(("fix purpose unknown: no supersedes ancestry to the design task",
                         [i for r in unlinked for i in r["openers"]]))
    if policy["problem"]:
        blockers.append(policy["problem"])
    return blockers


def _policy(slug, valid, cur, current, facts):
    """Conflicting declarations are history conflicts; a missing or failing policy is never satisfied."""
    sources = []
    for cycle in sorted({r["cycle"] for r in valid if r["cycle"] <= current}, reverse=True):
        # The deliverable's origin defines policy: builds, else designs, else an external review declaration.
        origin = [r for r in valid if r["cycle"] == cycle and not r["supersedes"]]
        sources = ([r for r in origin if r["stage"] == "build"] or [r for r in origin if r["stage"] == "design"]
                   or [r for r in origin if r["external"]])
        if sources:
            break
    evidence = [i for r in sources for i in r["openers"]]
    declared = {r["policy"] for r in sources if r["policy"]}
    result = {"conflict": None, "problem": None, "label": None}
    if len(declared) > 1:
        result["conflict"] = ("conflicting repository/check policies", evidence)
        return result
    if not sources or any(r["policy"] is None for r in sources):
        result["problem"] = ("check policy missing", evidence)  # section 2: missing is unknown, never empty
        return result
    gates, reason = declared.pop()[3:]
    if gates and reason:
        result["problem"] = ("no_gates_reason conflicts with required gates", evidence)
    elif not gates:
        result["label"] = "local checks not tracked" if reason else None
        result["problem"] = None if reason else ("check policy missing", evidence)
    else:
        passed = facts["checks"].get((slug, current))
        result["label"] = {True: "required checks green", False: "required checks failed"}.get(
            passed, "required check evidence unavailable")
        if passed is not True:
            result["problem"] = ("required checks not satisfied" if passed is False
                                 else "required check evidence unavailable", evidence)
    return result
