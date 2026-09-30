"""Worker-only board projection. HTTP copies a bounded, already reduced feed."""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from agenttalk import gates, work_board, work_board_facts
from agenttalk.envelope_snapshot import selected_closure
from agenttalk.store import Message


def _time(text):
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _closure(messages, slugs):
    """All cycles plus connected request, reply and replacement dependencies."""
    links, seeds = defaultdict(set), set()
    for m in messages:
        node = ("id", m.id)
        if isinstance(m.meta.get("work_item"), str) and m.meta["work_item"] in slugs:
            seeds.add(node)
        for key, namespace in (("request_id", "request"), ("supersedes", "request"), ("in_reply_to", "id")):
            value = m.meta.get(key)
            if isinstance(value, str):
                other = (namespace, value)
                links[node].add(other)
                links[other].add(node)
    todo, seen = list(seeds), set(seeds)
    while todo:
        for other in links[todo.pop()] - seen:
            seen.add(other)
            todo.append(other)
    return {value for namespace, value in seen if namespace == "id"}


def bounded(feed, *, card_limit=100, byte_limit=256 * 1024):
    """Omit whole cards, never present a truncated card as complete evidence."""
    total = max(len(feed["items"]), feed["total_count"] or 0)
    was_truncated = feed.get("truncated", False)
    feed["items"] = feed["items"][:card_limit]
    def counts():
        feed.update(truncated=was_truncated or len(feed["items"]) < total,
                    omitted_count=total - len(feed["items"]) if feed["total_count"] is not None else None)
    counts()
    while len(json.dumps(feed, indent=2, ensure_ascii=False).encode()) > byte_limit:
        if feed["items"]:
            feed["items"].pop()
            counts()
        elif not feed.get("groups_truncated"):
            feed["groups_truncated"] = True
            feed["legacy"]["examples"] = []
            feed["unassigned"].update(examples=[], reasons={})
            feed["errors"] = ["group details exceed response budget"]
        else:
            raise ValueError("board response budget too small for coverage summary")
    return feed


def build(snapshot, *, project, lead, gate_state, now=None, integrated=None, integration=None,
          envelope_limit=50000, source_byte_limit=128 * 1024 * 1024, **bounds):
    now = now or datetime.now(timezone.utc)
    # Identical active/compacted copies count once. Conflicts are rejected by closure coverage.
    found = {e.id: e for e in (*snapshot.archives, *snapshot.envelopes)}
    messages = [Message.from_dict(e.fields) for e in found.values()]
    preliminary = work_board.reduce(messages, lead=lead, integrated=integrated)
    notes, warnings = {}, list((integration or {}).get("warnings", []))
    if integration is not None:
        try:  # facts bind to each item's current repository binding from this reduction
            integrated, notes = work_board_facts.integration_for(preliminary["items"], integration)
        except Exception as exc:  # noqa: BLE001 - optional evidence never suppresses the board
            integrated, notes = {}, {}
            warnings.append(f"integration evidence unusable ({type(exc).__name__})")
    checks = {(i["work_item"], i["cycle"]): gates.check_board(
        None, project=project, item=i["work_item"], cycle=i["cycle"], revision=i["candidate"],
        keys=i.get("check_keys"), state=gate_state) for i in preliminary["items"]}
    reduced = work_board.reduce(messages, lead=lead, integrated=integrated, checks=checks)
    selected = []
    global_gates = gates.check_gates(None, state=gate_state)
    # Correlated untagged replies contribute to approximate activity dates too.
    request_items = {m.meta.get("request_id"): m.meta.get("work_item") for m in messages
                     if m.kind in ("task", "review-request") and isinstance(m.meta.get("request_id"), str)}
    by_id, assigned = {m.id: m for m in messages}, {}
    activity, dispatched = defaultdict(list), defaultdict(list)
    for m in messages:
        trail, cursor, slug = set(), m, None
        while cursor and cursor.id not in trail:
            trail.add(cursor.id)
            rid = cursor.meta.get("request_id")
            slug = (assigned.get(cursor.id) or cursor.meta.get("work_item")
                    or (request_items.get(rid) if isinstance(rid, str) else None))
            if isinstance(slug, str):
                assigned.update(dict.fromkeys(trail, slug))
                break
            anchor = cursor.meta.get("in_reply_to")
            cursor = by_id.get(anchor) if isinstance(anchor, str) else None
        when = _time(m.ts)
        if isinstance(slug, str) and when:
            activity[slug].append(when)
            if m.kind in ("task", "review-request"):
                dispatched[slug].append(when)
    for item in reduced["items"]:
        times = activity[item["work_item"]]
        last = max(times) if times else None
        obs = item["obligations"]
        cancelled = (item["reason"] == "all execution declined or cancelled" and obs
                     and all(o["state"] in ("declined", "rescinded") for o in obs)
                     and not any(c["outstanding"] for c in item["previous_cycles"]))
        if snapshot.archives_complete and not snapshot.invalid_count and not snapshot.archive_invalid_count and (
                cancelled or (item["workflow_column"] == "done" and last and last < now - timedelta(days=7))):
            continue
        note = notes.get((item["work_item"], item["candidate"]))
        if note:  # stale or unmatched evidence never makes Done; the derived column stays
            item["reason"] += "; " + note[0]
            if note[1]:
                item["integration_stale_as_of"] = note[1]
        starts = dispatched[item["work_item"]]
        item.update(first_dispatch_at=min(starts).isoformat() if starts else None,
                    last_work_event_at=last.isoformat() if last else None,
                    global_gates={"verdict": global_gates["verdict"], "blockers": global_gates["blockers"]})
        selected.append(item)
    closure = selected_closure(snapshot, _closure(messages, {i["work_item"] for i in selected}),
                               envelope_limit=envelope_limit, byte_limit=source_byte_limit)
    closure.pop("items")
    if reduced.get("error") and closure["status"] == "complete":
        closure["status"] = "incomplete"
    began = now - timedelta(seconds=snapshot.duration)
    closure.update(generation=snapshot.generation, scan_started_at=began.isoformat(),
                   scan_ended_at=now.isoformat(), valid_until=(began + timedelta(seconds=15)).isoformat())
    complete = closure["status"] == "complete" and not reduced.get("error")
    if not complete:
        for item in selected:
            item.update(column="unknown", workflow_column="unknown", reason="board coverage " + closure["status"])
        reduced["legacy"]["open_request_count"] = None
    selected.sort(key=lambda i: (i["last_work_event_at"] or "", i["work_item"]), reverse=True)
    errors = ([reduced["error"]] if reduced.get("error") else []) + global_gates.get("warnings", [])
    errors += warnings
    if closure["capacity_warning"]:
        errors.append("selected closure at capacity warning; schedule B4b/B4c indexing")
    return bounded({"schema_version": 1, "target_root_project_id": project, "generated_at": now.isoformat(),
                    "coverage": closure, "items": selected, "legacy": reduced["legacy"],
                    "unassigned": reduced["unassigned"], "total_count": len(selected) if complete else None,
                    "errors": errors, "window_days": 7}, **bounds)
