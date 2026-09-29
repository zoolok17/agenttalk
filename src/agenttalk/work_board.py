"""Pure work-board reducer (#207 slices B3a/B3b): validated envelopes in, work items out.

Correctness uses explicit correlation only: request_id/in_reply_to, supersedes
and declared cycle numbers. Message IDs and timestamps never order evidence.
Bodies and subjects are never read; seat names are compared as identities,
never parsed. Facts the bus cannot prove (operator incidents, integration,
fresh health, check results) are injected by later slices and default to unknown.
Before reduction, a read-side audit applies the publication invariants of
work_tags to every envelope, historical or modern. A violation is evidence
against the item the envelope can still be attributed to, which becomes Unknown;
an explicit reference that does not resolve is missing history, never a fallback;
unattributable violations are reported, not dropped. reduce() is total: an
internal fault on one item makes that item Unknown and the rest of the board reduces.
"""

import json

from agenttalk import gates, work_tags
from agenttalk.threads import _classify_event

EXECUTION = {"design", "build", "fix"}
EARLIER_REQUESTER = "outstanding task from an earlier requester; current lead cannot cancel yet"
EXTERNAL = "external deliverable; authorship unverified"
MISSING = "missing required correlation history"
FAULT = "reducer could not evaluate this item"
_TAGS = ("work_item", "stage", "work_cycle", "work_round", "external_deliverable")
_DISPATCH_ONLY = {"supersedes", "assignee_model_vendors", "assignee_model_vendor"}
_POLICY = ("work_repo", "work_branch", "work_target", "no_gates_reason")


def _tag(meta, key):
    return work_tags.value(key, meta[key]) if key in meta else None


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


def _request(rid, copies, responses, running, descends, parse=True):
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
    if not parse:
        return req
    try:
        req.update(_parse(meta)[1])
    except (TypeError, ValueError):
        req["issues"].append(("malformed work metadata", openers))
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
                try:
                    ob["verdict"], ob["issue"] = work_tags.reply_verdict(
                        reply.kind, req["stage"], reply.meta.get("status"), reply.meta.get("verdict"))
                except ValueError as exc:  # audited away for work items; legacy has no pairing
                    ob["issue"] = str(exc)
            # N1: publication does not order a result and a needs-info; only explicit reply links do.
            before = [h for h in ob["holds"] if descends(reply.id, h)]
            after = [h for h in ob["holds"] if descends(h, reply.id)]
            if set(before) | set(after) != set(ob["holds"]):
                req["issues"].append(("ambiguous response order", [reply.id, *ob["holds"]]))
            ob["hold_open"] = bool(after)
        req["obligations"].append(ob)
    return req


def _slug(meta):
    """The valid work-item tag of an envelope, else None (absent or malformed)."""
    try:
        return work_tags.value("work_item", meta["work_item"]) if "work_item" in meta else None
    except (TypeError, ValueError):
        return None


def _cyclic(start, parent):
    seen, node = {start}, parent.get(start)
    while node is not None:
        if node in seen:
            return True
        seen.add(node)
        node = parent.get(node)
    return False


def _audit(messages, openers):
    """Read-side publication invariants over every envelope, historical or modern, before reduction.

    The checks are work_tags' own (reply_request, reply_opener, inherit, reply_verdict, value) and the
    response-status enum. A violating envelope is withheld from reduction and recorded against whatever it
    can still be attributed to: its request, its reply ancestry, then its tag; otherwise it is unassigned.
    """
    by_id = {m.id: m for m in messages}
    parent = {m.id: m.meta["in_reply_to"] for m in messages if isinstance(m.meta.get("in_reply_to"), str)}
    responses, flags, unassigned = {}, {}, []

    def owners(m):
        # Every reference counts independently: request, whole reply ancestry, replacement target, own tag.
        found = set()
        references = [m.meta.get("request_id")]
        if m.kind in work_tags.OPENERS:
            references.append(m.meta.get("supersedes"))
        seen, node = set(), m.meta.get("in_reply_to")
        while isinstance(node, str) and node in by_id and node not in seen:
            seen.add(node)
            references.append(by_id[node].meta.get("request_id"))
            node = parent.get(node)
        found.update(ref for ref in references if isinstance(ref, str) and ref in openers)
        slug = _slug(m.meta)
        return sorted(found) + ([("item", slug)] if slug else [])

    def flag(m, reason):
        targets = owners(m)
        if not targets:
            unassigned.append((reason, m.id))
        for target in targets:
            flags.setdefault(target, []).append((reason, [m.id]))

    for m in messages:
        try:
            _audit_one(m, by_id, parent, openers, responses, flag)
        except Exception as exc:  # noqa: BLE001 - totality: the fault becomes that item's evidence
            flag(m, f"{FAULT} ({type(exc).__name__})")
    return responses, flags, unassigned, parent


def _audit_one(m, by_id, parent, openers, responses, flag):
    meta = m.meta
    rid, anchor_id = meta.get("request_id"), meta.get("in_reply_to")
    if (not (rid is None or isinstance(rid, str)) or not (anchor_id is None or isinstance(anchor_id, str))
            or (m.kind in work_tags.OPENERS and not rid)):
        flag(m, "malformed correlation")
    elif anchor_id is not None and anchor_id not in by_id:
        flag(m, MISSING)  # a named reference that does not resolve is never replaced by a fallback
    elif anchor_id is not None and _cyclic(m.id, parent):
        flag(m, "cyclic reply ancestry")
    elif m.kind in work_tags.OPENERS:
        try:  # malformed tags are reported by the per-request parse; replay the rest of publication
            clean = {k: work_tags.value(k, v) if k in work_tags.FIELDS else v for k, v in meta.items()}
        except (TypeError, ValueError):
            return
        try:
            if clean.get("external_deliverable") is True:
                work_tags.external_declaration(m.kind, clean)
            target = clean.get("supersedes")
            if isinstance(target, str) and target in openers:  # a missing target is the graph's MISSING
                original = work_tags.replacement_target(clean, openers[target])
                if work_tags.inherit_dispatch(original.meta, clean) != clean:
                    raise ValueError("replacement dispatch lost its original's declaration or policy")
        except (TypeError, ValueError) as exc:
            flag(m, str(exc))
    elif m.kind in work_tags.REPLIES or m.kind == "rescind":
        anchor = by_id.get(anchor_id)
        request = rid or (anchor.meta.get("request_id") if anchor is not None else None)
        copies = openers.get(request) if isinstance(request, str) else None
        if copies is None:
            flag(m, MISSING)
        elif m.kind == "rescind" or not (any("work_item" in c.meta for c in copies)
                                         or set(work_tags.FIELDS) & meta.keys()):
            responses.setdefault(request, []).append(m)  # untagged legacy protocol is unchanged, as published
        else:
            try:
                if _DISPATCH_ONLY & meta.keys():
                    raise ValueError("reply carries dispatch-only metadata")
                work_tags.reply_request(meta, anchor)
                opener = work_tags.reply_opener(copies, m.sender, m.recipient, m.kind)
                clean = work_tags.inherit(opener.meta, {
                    k: work_tags.value(k, v) if k in work_tags.FIELDS else v for k, v in meta.items()})
                gates.validate_response_status(m.kind, clean)
                _, issue = work_tags.reply_verdict(m.kind, clean.get("stage"), clean.get("status"),
                                                   clean.get("verdict"))
                if issue == "unrecognized verdict":  # uninterpretable evidence cannot certify any row
                    raise ValueError(issue)
            except LookupError:
                flag(m, MISSING)
            except (TypeError, ValueError) as exc:
                flag(m, str(exc))
            else:
                responses.setdefault(request, []).append(m)


def reduce(messages, *, lead, incidents=(), integrated=None, running=frozenset(), checks=None):
    """Return {"items", "legacy", "unassigned"}; total, never mutates its input or orders it by ID."""
    try:
        return _reduce(messages, lead, incidents, integrated, running, checks)
    except Exception as exc:  # noqa: BLE001 - last resort; per-item isolation below normally applies
        return {"items": [], "legacy": {"open_request_count": None, "known_lower_bound": 0, "counts_by_kind": {},
                                        "examples": [], "truncated": False},
                "unassigned": {"count": 0, "reasons": {}, "examples": []},
                "error": f"reducer could not evaluate the board ({type(exc).__name__})"}


def _well_formed(m):
    return all(isinstance(getattr(m, name, None), str) for name in ("id", "kind", "sender", "recipient")) and \
        isinstance(getattr(m, "meta", None), dict)


def _reduce(messages, lead, incidents, integrated, running, checks):
    messages = list(messages)
    broken = [m for m in messages if not _well_formed(m)]
    messages = [m for m in messages if _well_formed(m)]
    openers = {}
    for m in messages:
        rid, anchor = m.meta.get("request_id"), m.meta.get("in_reply_to")
        if m.kind in work_tags.OPENERS and isinstance(rid, str) and rid and (anchor is None or isinstance(anchor, str)):
            openers.setdefault(rid, []).append(m)
    responses, flags, unassigned, parent = _audit(messages, openers)
    unassigned += [("malformed envelope", getattr(m, "id", None)) for m in broken]

    def descends(later, earlier):
        seen, node = set(), parent.get(later)
        while node is not None and node not in seen:
            if node == earlier:
                return True
            seen.add(node)
            node = parent.get(node)
        return False

    grouped, legacy, detached = {}, [], []
    for rid, copies in openers.items():
        try:
            req = _request(rid, copies, responses.get(rid, []), running, descends)
        except Exception as exc:  # noqa: BLE001 - totality: the request's item becomes Unknown
            req = _request(rid, copies, [], frozenset(), lambda later, earlier: False, parse=False)
            req["issues"].append((f"{FAULT} ({type(exc).__name__})", req["openers"]))
        req["issues"] += flags.get(rid, [])
        req["work_item"] = _slug(copies[0].meta)
        if req["work_item"]:
            grouped.setdefault(req["work_item"], []).append(req)
        elif "work_item" in copies[0].meta:
            detached.append((req, copies))
        else:
            legacy.append(req)
    item_of = {r["request_id"]: slug for slug, reqs in grouped.items() for r in reqs}
    for req, copies in detached:
        ids = {c.id for c in copies}
        target = copies[0].meta.get("supersedes")
        links = {item_of.get(target)} if isinstance(target, str) else set()
        links |= {_slug(m.meta) for m in messages if m.meta.get("request_id") == req["request_id"]
                  or (isinstance(m.meta.get("in_reply_to"), str) and m.meta["in_reply_to"] in ids)}
        links.discard(None)
        if len(links) == 1:
            grouped.setdefault(links.pop(), []).append(req)  # its malformed-metadata issue makes that item Unknown
        else:
            unassigned.extend(("malformed work metadata", i) for i in sorted(ids))
    tagged = {key[1]: issues for key, issues in flags.items() if isinstance(key, tuple)}
    facts = {"lead": lead, "incidents": list(incidents), "integrated": integrated or {}, "checks": checks or {},
             "known": set(openers)}
    items = [_item(slug, grouped.get(slug, []), tagged.get(slug, []), facts)
             for slug in sorted(set(grouped) | set(tagged))]
    reasons = {}
    for reason, _ in unassigned:
        reasons[reason] = reasons.get(reason, 0) + 1
    try:
        legacy_group = _legacy(legacy)
    except Exception as exc:  # noqa: BLE001 - totality: an uncountable legacy group is unknown, never zero
        legacy_group = {"open_request_count": None, "known_lower_bound": 0, "counts_by_kind": {}, "examples": [],
                        "truncated": False, "error": f"{FAULT} ({type(exc).__name__})"}
    return {"items": items, "legacy": legacy_group,
            "unassigned": {"count": len(unassigned), "reasons": dict(sorted(reasons.items())),
                           "examples": sorted(i for _, i in unassigned)[:20]}}


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
            why = "supersedes crosses work items" if target in known else MISSING
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
    try:
        return _evaluate_item(slug, reqs, orphans, facts)
    except Exception as exc:  # noqa: BLE001 - totality: one item's fault never hides the rest of the board
        evidence = sorted({i for r in reqs for i in r["openers"]} | {i for _, ids in orphans for i in ids})
        return {"work_item": slug, "title": None, "cycle": None, "legacy_cycle": True, "round": None,
                "candidate": None, "builders": [], "verdicts": {}, "obligations": [], "incidents": [],
                "issues": [], "previous_cycles": [], "checks": None, "integration": {},
                "work_target": None, "work_repo": None,
                "column": "unknown", "workflow_column": "unknown", "row": 2,
                "reason": f"{FAULT} ({type(exc).__name__})", "evidence": evidence}


def _evaluate_item(slug, reqs, orphans, facts):
    item = {"work_item": slug, "title": None, "cycle": None, "legacy_cycle": True, "round": None,
            "candidate": None, "builders": [], "verdicts": {}, "obligations": [], "incidents": [],
            "issues": [], "previous_cycles": [], "checks": None, "work_target": None, "work_repo": None,
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
        "conflict": None, "problem": ("check policy missing", []), "label": None,
        "target": None, "repo": None}
    item["checks"] = policy["label"]
    item["check_keys"] = policy.get("keys")
    item["work_target"] = policy.get("target")  # B5: declared merge-target branch, else None
    item["work_repo"] = policy.get("repo")  # B5: declared work_repo alias, else None
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
        # A policy conflict is often derivative (e.g. of M3), so the specific history conflict leads.
        conflicts.sort(key=lambda c: (c[0] == "conflicting repository/check policies", c[0], sorted(c[1])))
        item["issues"] += sorted({reason for reason, _ in conflicts} - set(item["issues"]))
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
    blockers = _blockers(cur, obs, execs, surviving, successor, candidate, heads, builders,
                         open_marks, descends_from_design, policy)
    if candidate and facts["integrated"].get((slug, candidate)) and not any(
            o["state"] == "outstanding" for o in execs):
        # Integration adds Done, never cleanliness: every obligation that would block Ready stays explicit,
        # so a clean Done is exactly an integrated Ready (design row 3; residual point 6).
        independent = any(o["verdict"] == "GO" and o["head"] == candidate and o["recipient"] not in builders
                          for r in surviving for o in _live(r))
        pending_review = any(o["state"] == "outstanding" for r in reviews for o in r["obligations"])
        delivered = any(r["external"] for r in cur) or any(
            o["state"] == "done" and o["verdict"] == "done" for r in cur
            if r["stage"] in EXECUTION and r["request_id"] not in successor for o in _live(r))
        reason = ("merged with open FIX/HOLD" if open_marks else "integrated without independent GO" if not independent
                  else "integrated without a recorded deliverable" if not delivered
                  else "integrated with review outstanding" if pending_review
                  else "integrated with failed required checks" if item["checks"] == "required checks failed"
                  else "integrated; " + blockers[0][0] if blockers else "integrated in configured target")
        item["issues"] += sorted({why for why, _ in blockers} - set(item["issues"]))
        evidence = [reply for _, reply in open_marks] or [i for r in surviving for i in r["openers"]]
        return 3, "done", reason, evidence + [i for _, ids in blockers for i in ids if i]
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
        # Every independent origin defines policy together: designs, builds and external review declarations.
        sources = [r for r in valid if r["cycle"] == cycle and not r["supersedes"]
                   and (r["stage"] in ("design", "build") or r["external"])]
        if sources:
            break
    evidence = [i for r in sources for i in r["openers"]]
    declared = {r["policy"] for r in sources if r["policy"]}
    result = {"conflict": None, "problem": None, "label": None, "target": None, "repo": None}
    if len(declared) > 1:
        result["conflict"] = ("conflicting repository/check policies", evidence)
        return result
    if not sources or any(r["policy"] is None for r in sources):
        result["problem"] = ("check policy missing", evidence)  # section 2: missing is unknown, never empty
        return result
    repo, _branch, target, gates, reason = declared.pop()
    # B5: the declared work_repo/work_target, surfaced for the integration fact seam (design
    # section 4: work_repo selects an operator-approved local alias, never a raw path).
    result["repo"], result["target"] = repo, target
    result["keys"] = list(gates) if gates is not None else None
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
