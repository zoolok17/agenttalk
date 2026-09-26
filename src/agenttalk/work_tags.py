"""Validated work-item publication metadata; no board state or prose inference."""

import re

FIELDS = ("work_item", "stage", "work_cycle", "work_round", "work_head", "supersedes", "work_title")
STAGES = {"design", "build", "read", "fix", "delta", "sweep"}
REVIEWS = {"read", "delta", "sweep"}
OPENERS = {"task", "review-request"}
REPLIES = {"task-response": "task", "review-result": "review-request"}


def value(key, raw):
    if key in {"work_cycle", "work_round"} and type(raw) is int:
        raw = str(raw)
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be text")
    if key in {"work_cycle", "work_round"}:
        if not re.fullmatch(r"[0-9]+", raw) or not raw.lstrip("0"):
            raise ValueError(f"{key} must be a positive decimal integer")
        return raw.lstrip("0")
    if key == "work_item" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", raw):
        raise ValueError("work_item must be a lowercase slug of at most 64 characters")
    if key == "stage" and raw not in STAGES:
        raise ValueError("stage must be design, build, read, fix, delta or sweep")
    if key == "work_head" and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", raw):
        raise ValueError("work_head must be a full Git OID")
    if key == "work_title" and (not raw or len(raw) > 160):
        raise ValueError("work_title must contain 1 to 160 characters")
    if key == "supersedes" and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", raw):
        raise ValueError("supersedes must name a request ID")
    return raw.lower() if key == "work_head" else raw


def task_metadata(meta, args):
    result = dict(meta)
    for key in FIELDS:
        flag = getattr(args, key, None)
        if flag is not None:
            if key in result and value(key, result[key]) != value(key, flag):
                raise ValueError(f"conflicting {key} flag and metadata")
            result[key] = flag
    return result


def _opener(messages, request_id):
    matches = [m for m in messages if m.kind in OPENERS and m.meta.get("request_id") == request_id]
    if not request_id or not matches:
        raise ValueError("work reference has no available opener on this root")
    first = matches[0]
    if (any((m.sender, m.kind, m.meta) != (first.sender, first.kind, first.meta) for m in matches)
            or len({m.recipient for m in matches}) != len(matches)):
        raise ValueError("work reference has ambiguous openers")
    return matches


def validate_replacement(meta, sender, messages, authorities):
    """Also called under publication lock: competing replacements cannot both land."""
    target = meta.get("supersedes")
    if target is None:
        return
    rid = meta.get("request_id")
    if not rid or rid == target or any(m.meta.get("request_id") == rid for m in messages):
        raise ValueError("replacement requires a fresh request ID; self-links/cycles are invalid")
    original = _opener(messages, target)[0]
    if not meta.get("work_item") or meta["work_item"] != original.meta.get("work_item"):
        raise ValueError("supersedes must name the same work item")
    if value("work_cycle", meta.get("work_cycle", "1")) != value("work_cycle", original.meta.get("work_cycle", "1")):
        raise ValueError("supersedes must name the same work cycle")
    if sender not in {original.sender, *authorities}:
        raise ValueError("replacement requires original dispatch authority or current lead/liaison")
    if any(m.kind in OPENERS and m.meta.get("supersedes") == target for m in messages):
        raise ValueError("ambiguous branching replacement; replace the surviving dispatch instead")
    return original


def normalize(store, sender, recipient, kind, meta):
    result = dict(meta)
    for key in FIELDS:
        if key == "supersedes" and kind == "rescind":
            continue  # Existing exact-generation rescind namespace; B2c is separate.
        if key in result:
            result[key] = value(key, result[key])
    if "supersedes" in result and kind != "rescind":
        if kind not in OPENERS:
            raise ValueError("supersedes belongs on a replacement dispatch")
        original = validate_replacement(
            result, sender, store.valid_messages(), (store.sole_lead(), store.operator_facing()),
        )
        for key in ("work_repo", "work_branch", "work_target", "required_gates", "no_gates_reason"):
            if key in original.meta:
                if key in result and result[key] != original.meta[key]:
                    raise ValueError(f"replacement {key} contradicts original dispatch policy")
                result[key] = original.meta[key]
    if kind not in REPLIES:
        return result
    messages = store.valid_messages()
    rid = result.get("request_id")
    anchor = next((m for m in messages if m.id == result.get("in_reply_to")), None)
    if not rid and anchor is not None:
        rid = anchor.meta.get("request_id")
    candidates = [m for m in messages if m.kind in OPENERS and m.meta.get("request_id") == rid]
    if not any("work_item" in m.meta for m in candidates) and not any(k in result for k in FIELDS):
        return result  # Untagged legacy protocol is unchanged.
    matches = _opener(messages, rid)
    opener = next((m for m in matches if m.recipient == sender), None)
    if (opener is None or opener.sender != recipient or opener.kind != REPLIES[kind]
            or (result.get("in_reply_to") and anchor is None)
            or (anchor is not None and anchor.meta.get("request_id") != rid)):
        raise ValueError("work reply participant, kind or correlation contradicts opener")
    for key in ("work_item", "stage", "work_cycle", "work_round", "work_head"):
        expected = opener.meta.get(key, "1" if key == "work_cycle" else None)
        if expected is not None:
            expected = value(key, expected)
        # Builders can report their output; a review must remain on its pinned OID.
        if key == "work_head" and opener.meta.get("stage") in {"design", "build", "fix"} and key in result:
            continue
        if key in result and result[key] != expected:
            raise ValueError(f"reply {key} contradicts opener")
        if key in opener.meta:
            result[key] = expected
    result.pop("verdict_issue", None)
    verdict = result.get("verdict")
    review = result.get("stage") in REVIEWS
    allowed = {"go": "GO", "fix": "FIX", "hold": "HOLD"} if review else (
        {"done": "done"} if result.get("stage") in STAGES else {})
    canonical = allowed.get(verdict.casefold()) if isinstance(verdict, str) else None
    if canonical is None:
        result["verdict_issue"] = "verdict missing" if verdict is None else "unrecognized verdict"
    else:
        result["verdict"] = canonical
        if kind == "review-result" and (result.get("status"), canonical) not in {
            ("approved", "GO"), ("rejected", "FIX"), ("rejected", "HOLD"), ("needs-info", "HOLD")
        }:
            raise ValueError("review status and verdict must agree")
    return result
