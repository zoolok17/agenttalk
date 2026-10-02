"""Validated work-item publication metadata; no board state or prose inference."""

import json
import re

FIELDS = ("work_item", "stage", "work_cycle", "work_round", "work_head", "supersedes", "work_title",
          "external_deliverable")
STAGES = {"design", "build", "read", "fix", "delta", "sweep"}
REVIEWS = {"read", "delta", "sweep"}
OPENERS = {"task", "review-request"}
REPLIES = {"task-response": "task", "review-result": "review-request"}
MODEL_VENDORS = {"anthropic", "openai", "alibaba", "other", "unverified"}

# issue #279: `planned` keys live OUTSIDE work_tags.FIELDS on purpose - FIELDS
# validation applies to every kind, and a global new key (e.g. a hypothetical
# `owner`) could reject metadata an unrelated kind already uses that name for.
# work_item/work_title ARE in FIELDS already and get that generic validation
# for free; `replaces`/`withdrawn` are planned-only and validated here.
PLANNED_FIELDS = frozenset({"work_item", "work_title", "replaces", "withdrawn"})


class PlannedRefused(ValueError):
    """A `planned` record's metadata fails the shared structural contract -
    raised identically at publish time (work_tags.normalize) and at replay
    time (work_board.planned_state), so the two can never disagree about
    what counts as a valid planned record."""


def validate_planned(meta):
    """The ONE structural validator for `planned` metadata (design
    docs/DESIGN-planned-stage.md §3-4): exactly the allowed keys, a valid
    work_item, and the add/change vs. withdraw shape. Never inspects
    `replaces` for WHICH record it names or whether that record exists -
    that is a graph question `work_board.planned_state` answers over the
    whole log, not a per-record shape question. Returns a clean dict
    (never echoes an input key this function didn't explicitly validate)."""
    if not isinstance(meta, dict):
        raise PlannedRefused("planned metadata must be an object")
    extra = set(meta) - PLANNED_FIELDS
    if extra:
        raise PlannedRefused(
            f"planned record carries unsupported metadata: {sorted(extra)}"
        )
    if "work_item" not in meta:
        raise PlannedRefused("planned record requires work_item")
    work_item = value("work_item", meta["work_item"])
    withdrawn = meta.get("withdrawn")
    if "withdrawn" in meta and withdrawn is not True:
        raise PlannedRefused("withdrawn must be true when present")
    clean = {"work_item": work_item}
    if withdrawn is True:
        if "work_title" in meta:
            raise PlannedRefused("a withdrawal record must not carry work_title")
        clean["withdrawn"] = True
    else:
        if "work_title" not in meta:
            raise PlannedRefused("planned record requires work_title")
        title = meta["work_title"]
        if not isinstance(title, str) or title.strip() == "":
            # work_tags.value's own work_title rule rejects empty but NOT
            # whitespace-only text - planned records are stricter (design
            # doc §3's explicit nonblank rule), without changing value()
            # itself for every other kind that already relies on it.
            raise PlannedRefused("planned record requires a nonblank work_title")
        clean["work_title"] = value("work_title", title)
    if "replaces" in meta:
        replaces = meta["replaces"]
        if not isinstance(replaces, str) or replaces.strip() == "" or len(replaces) > 128:
            raise PlannedRefused("replaces must name a nonblank message id")
        clean["replaces"] = replaces
    return clean


def validate_vendor_config(mapping, roster):
    if (not isinstance(mapping, dict) or set(mapping) - set(roster)
            or any(not isinstance(v, str) or v not in MODEL_VENDORS for v in mapping.values())):
        raise ValueError("model_vendor must map roster members to a supported vendor")
    return mapping


def vendor_snapshot(cfg, recipients):
    mapping = validate_vendor_config(cfg.get("model_vendor", {}), cfg["agents"])
    return {name: mapping.get(name, "unverified") for name in recipients}


def validate_vendor_map(mapping, recipients):
    validate_vendor_config(mapping, recipients)
    if set(mapping) != set(recipients):
        raise ValueError("assignee_model_vendors must name the exact dispatch recipients")
    return dict(mapping)


def reject_vendor_override(meta):
    if {"assignee_model_vendors", "assignee_model_vendor"} & meta.keys():
        raise ValueError("assignee_model_vendors is publisher-owned; vendor metadata overrides are forbidden")


def item_ref(meta):
    """Legacy malformed links remain team attention, never guessed item placement."""
    try:
        return {"work_item": value("work_item", meta.get("work_item")),
                "work_cycle": value("work_cycle", meta.get("work_cycle", "1"))}
    except ValueError:
        return {}


def value(key, raw):
    if key == "external_deliverable":
        if type(raw) is bool:
            return raw
        if isinstance(raw, str) and raw in {"true", "false"}:
            return raw == "true"
        raise ValueError("external_deliverable must be true or false")
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
    original = replacement_target(meta, messages)
    if sender not in {original.sender, *authorities}:
        raise ValueError("replacement requires original dispatch authority or current lead/liaison")
    if any(m.kind in OPENERS and m.meta.get("supersedes") == target for m in messages):
        raise ValueError("ambiguous branching replacement; replace the surviving dispatch instead")
    return original


def replacement_target(meta, messages):
    """The original a replacement names, when item and cycle agree (structure only; the board audit
    replays this over history, where freshness and lead authority cannot be reconstructed)."""
    original = _opener(messages, meta.get("supersedes"))[0]
    if not meta.get("work_item") or meta["work_item"] != original.meta.get("work_item"):
        raise ValueError("supersedes must name the same work item")
    if value("work_cycle", meta.get("work_cycle", "1")) != value("work_cycle", original.meta.get("work_cycle", "1")):
        raise ValueError("supersedes must name the same work cycle")
    return original


def _external_opener(store, sender, kind, meta):
    if sender != store.sole_lead():
        raise ValueError("external deliverable requires a lead-issued review dispatch")
    external_declaration(kind, meta)


def external_declaration(kind, meta):
    """Structural requirements of an external declaration; the board audit replays them over history.

    Lead authority is checked only at publication (_external_opener): past roles are not recorded.
    """
    if kind not in OPENERS or meta.get("stage") not in REVIEWS:
        raise ValueError("external deliverable requires a lead-issued review dispatch")
    for key in ("work_item", "work_head", "work_repo", "work_branch", "work_target"):
        if not isinstance(meta.get(key), str) or not meta[key].strip() or len(meta[key]) > 256:
            raise ValueError(f"external deliverable requires bounded {key}")
    gates = meta.get("required_gates")
    if isinstance(gates, str) and len(gates) <= 4096:
        try:
            gates = json.loads(gates)
        except ValueError:
            gates = None  # same validated refusal as a non-array value
    if (not isinstance(gates, list) or len(gates) > 64
            or any(not isinstance(g, str) or not re.fullmatch(r"[a-z0-9-]{1,24}", g) for g in gates)):
        raise ValueError("external deliverable requires an explicit check-key array (at most 64 keys)")
    reason = meta.get("no_gates_reason")
    if (gates and reason is not None) or (not gates and (
            not isinstance(reason, str) or not reason.strip() or len(reason) > 1024)):
        raise ValueError("empty checks require a bounded no_gates_reason; nonempty checks forbid it")


REVIEW_PAIRS = frozenset({("approved", "GO"), ("rejected", "FIX"), ("rejected", "HOLD"), ("needs-info", "HOLD")})
INHERITED = ("work_item", "stage", "work_cycle", "work_round", "work_head", "external_deliverable")


# Publication invariants shared with the board's read-side audit (work_board), so history is judged by the
# same rules modern publication enforces.
def inherit_dispatch(original_meta, meta):
    """Replacement dispatch metadata carrying its original's external declaration and repository/check policy."""
    result = dict(meta)
    expected_external = value("external_deliverable", original_meta.get("external_deliverable", False))
    if result.get("external_deliverable", expected_external) != expected_external:
        raise ValueError("replacement external_deliverable contradicts original declaration")
    if "external_deliverable" in original_meta:
        result["external_deliverable"] = expected_external
    for key in ("work_repo", "work_branch", "work_target", "required_gates", "no_gates_reason"):
        if key in original_meta:
            if key in result and result[key] != original_meta[key]:
                raise ValueError(f"replacement {key} contradicts original dispatch policy")
            result[key] = original_meta[key]
    return result


def reply_request(meta, anchor):
    """Request a reply answers; LookupError for a named but absent anchor, ValueError for a disagreeing one."""
    rid = meta.get("request_id")
    if not rid and anchor is not None:
        rid = anchor.meta.get("request_id")
    if meta.get("in_reply_to") and anchor is None:
        raise LookupError("missing required correlation history")
    if anchor is not None and anchor.meta.get("request_id") != rid:
        raise ValueError("reply request_id contradicts its in_reply_to anchor")
    return rid


def reply_opener(matches, sender, recipient, kind):
    """The fan-out copy a work reply answers; ValueError when participants or kinds contradict it."""
    opener = next((m for m in matches if m.recipient == sender), None)
    if opener is None or opener.sender != recipient or opener.kind != REPLIES[kind]:
        raise ValueError("work reply participant, kind or correlation contradicts opener")
    return opener


def inherit(opener_meta, meta):
    """Reply metadata with the opener's normalized tags; ValueError when the reply contradicts them."""
    result = dict(meta)
    for key in INHERITED:
        expected = opener_meta.get(key, {"work_cycle": "1", "external_deliverable": False}.get(key))
        if expected is not None:
            expected = value(key, expected)
        # Builders can report their output; a review must remain on its pinned OID.
        if key == "work_head" and opener_meta.get("stage") in {"design", "build", "fix"} and key in result:
            continue
        if key in result and result[key] != expected:
            raise ValueError(f"reply {key} contradicts opener")
        if key in opener_meta:
            result[key] = expected
    return result


def reply_verdict(kind, stage, status, raw):
    """Canonical verdict or its issue; ValueError when a native review's status contradicts its verdict."""
    allowed = {"go": "GO", "fix": "FIX", "hold": "HOLD"} if stage in REVIEWS else (
        {"done": "done"} if stage in STAGES else {})
    canonical = allowed.get(raw.casefold()) if isinstance(raw, str) else None
    if canonical is None:
        return None, "verdict missing" if raw is None else "unrecognized verdict"
    if kind == "review-result" and (status, canonical) not in REVIEW_PAIRS:
        raise ValueError("review status and verdict must agree")
    return canonical, None


def normalize(store, sender, recipient, kind, meta):
    reject_vendor_override(meta)
    if kind == "planned":
        # Self-contained: a planned record never goes through the generic
        # FIELDS loop, reply/opener machinery, or external-deliverable
        # checks below - none of them apply to it, and routing it through
        # them would validate fields (stage, supersedes, ...) this record
        # must never carry in the first place (design doc §3).
        return validate_planned(meta)
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
        result = inherit_dispatch(original.meta, result)
    if result.get("external_deliverable") is True and kind not in REPLIES:
        _external_opener(store, sender, kind, result)
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
    try:
        reply_request(result, anchor)
        opener = reply_opener(matches, sender, recipient, kind)
    except (LookupError, ValueError):
        raise ValueError("work reply participant, kind or correlation contradicts opener") from None
    result = inherit(opener.meta, result)
    result.pop("verdict_issue", None)
    canonical, issue = reply_verdict(kind, result.get("stage"), result.get("status"), result.get("verdict"))
    if canonical is None:
        result["verdict_issue"] = issue
    else:
        result["verdict"] = canonical
    return result
