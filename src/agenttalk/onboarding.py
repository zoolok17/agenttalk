"""Project onboarding ledger.

The onboarding layer records the first pass over a new or existing codebase:
which areas were assigned, what claims were learned, where documentation and
code disagree, and which unknowns still block confident implementation.

It is deliberately advisory. Agenttalk stores pointer-shaped evidence and
workflow state, not copied source or raw bus transcripts.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agenttalk import domains as dom

SCHEMA_VERSION = 1
STORE_DIRNAME = "onboarding"
EVENTS_FILENAME = "events.jsonl"

EVENT_CREATE = "create"
EVENT_STATE = "state"
EVENT_RECORD = "record"

RUN_STATES = frozenset({
    "planned",
    "scanning",
    "reconciling",
    "ready-for-work",
    "active",
    "blocked",
    "closed",
    "superseded",
    "abandoned",
})
TERMINAL_RUN_STATES = frozenset({"closed", "superseded", "abandoned"})

KIND_SEGMENT = "segment"
KIND_CLAIM = "claim"
KIND_DRIFT = "drift"
KIND_UNKNOWN = "unknown"
ITEM_KINDS = frozenset({KIND_SEGMENT, KIND_CLAIM, KIND_DRIFT, KIND_UNKNOWN})

SEGMENT_STATUSES = frozenset({
    "assigned",
    "reading",
    "submitted",
    "checking",
    "accepted",
    "rework",
    "blocked",
})
CLAIM_STATUSES = frozenset({
    "proposed",
    "confirmed",
    "conflicted",
    "superseded",
    "needs-human",
})
DRIFT_STATUSES = frozenset({
    "open",
    "triaged",
    "accepted-doc-bug",
    "accepted-code-bug",
    "intentional",
    "resolved",
    "deferred",
})
UNKNOWN_STATUSES = frozenset({"open", "answered", "deferred"})
STATUS_BY_KIND = {
    KIND_SEGMENT: SEGMENT_STATUSES,
    KIND_CLAIM: CLAIM_STATUSES,
    KIND_DRIFT: DRIFT_STATUSES,
    KIND_UNKNOWN: UNKNOWN_STATUSES,
}

CLAIM_SOURCES = frozenset({"code", "docs", "test", "command", "human", "ci", "runtime"})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})

SUMMARY_MAX_BYTES = 800
TITLE_MAX_BYTES = 200
OBJECTIVE_MAX_BYTES = 1000
REF_MAX_BYTES = 200
LIST_LIMIT = 64

_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_AGENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class OnboardingError(ValueError):
    """Invalid onboarding input or state."""


def new_run_id() -> str:
    return "ob-" + uuid.uuid4().hex[:12]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_run_id(value: object) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.match(value):
        raise OnboardingError(
            f"run id {value!r} is not safe (alphanumeric plus . _ -, starts alphanumeric, max 64 chars)"
        )
    return value


def validate_key(value: object) -> str:
    if not isinstance(value, str) or not _KEY_RE.match(value):
        raise OnboardingError(
            f"key {value!r} is not safe (alphanumeric plus . _ : -, starts alphanumeric, max 128 chars)"
        )
    return value


def validate_agent(value: object, *, field: str = "agent", required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not _AGENT_RE.match(value):
        raise OnboardingError(f"{field} must be a safe agent name")
    return value


def validate_run_state(value: object) -> str:
    if value not in RUN_STATES:
        raise OnboardingError(f"state must be one of {sorted(RUN_STATES)}, got {value!r}")
    return str(value)


def validate_kind(value: object) -> str:
    if value not in ITEM_KINDS:
        raise OnboardingError(f"kind must be one of {sorted(ITEM_KINDS)}, got {value!r}")
    return str(value)


def validate_status(kind: str, value: object) -> str:
    allowed = STATUS_BY_KIND[kind]
    if value not in allowed:
        raise OnboardingError(
            f"{kind} status must be one of {sorted(allowed)}, got {value!r}"
        )
    return str(value)


def _bounded_text(value: object, field: str, *, max_bytes: int,
                  required: bool = True) -> str:
    if value is None:
        if required:
            raise OnboardingError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise OnboardingError(f"{field} must be text")
    out = value.replace("\r", "\n").strip()
    if not out and required:
        raise OnboardingError(f"{field} is required")
    n = len(out.encode("utf-8"))
    if n > max_bytes:
        raise OnboardingError(f"{field} is {n} bytes, above the {max_bytes}-byte cap")
    return out


def _norm_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OnboardingError("path must be a non-empty repo-relative path")
    try:
        return dom.normalize_repo_path(value)
    except dom.DomainError as e:
        raise OnboardingError(f"path {value!r} is not safe repo-relative path: {e}") from e


def _bounded_list(values: list[str] | None, field: str, *, max_bytes: int,
                  limit: int = LIST_LIMIT) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise OnboardingError(f"{field} must be a list")
    out: list[str] = []
    for raw in values:
        text = _bounded_text(raw, field, max_bytes=max_bytes)
        out.append(text)
    if len(out) > limit:
        raise OnboardingError(f"{field} may contain at most {limit} entries")
    return out


def _agent_list(values: list[str] | None, field: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise OnboardingError(f"{field} must be a list")
    out = [validate_agent(v, field=field) for v in values]
    if len(out) > LIST_LIMIT:
        raise OnboardingError(f"{field} may contain at most {LIST_LIMIT} entries")
    return out


def _path_list(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise OnboardingError("path must be a list")
    out = [_norm_path(v) for v in values]
    if len(out) > LIST_LIMIT:
        raise OnboardingError(f"path may contain at most {LIST_LIMIT} entries")
    return out


def new_create_event(*, run_id: str, title: str, objective: str | None,
                     base_ref: str | None, lead: str, state: str,
                     at: str | None = None) -> dict[str, Any]:
    now = at or utc_now()
    evt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_CREATE,
        "run_id": validate_run_id(run_id),
        "title": _bounded_text(title, "title", max_bytes=TITLE_MAX_BYTES),
        "lead": validate_agent(lead, field="lead"),
        "state": validate_run_state(state),
        "created_at": now,
        "updated_at": now,
    }
    obj = _bounded_text(
        objective, "objective", max_bytes=OBJECTIVE_MAX_BYTES, required=False)
    if obj:
        evt["objective"] = obj
    base = _bounded_text(base_ref, "base_ref", max_bytes=REF_MAX_BYTES, required=False)
    if base:
        evt["base_ref"] = base
    return evt


def new_state_event(*, run_id: str, state: str, actor: str,
                    summary: str | None = None, at: str | None = None) -> dict[str, Any]:
    evt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_STATE,
        "run_id": validate_run_id(run_id),
        "state": validate_run_state(state),
        "actor": validate_agent(actor),
        "updated_at": at or utc_now(),
    }
    text = _bounded_text(summary, "summary", max_bytes=SUMMARY_MAX_BYTES, required=False)
    if text:
        evt["summary"] = text
    return evt


def new_record_event(*, run_id: str, kind: str, key: str, status: str,
                     summary: str, actor: str, segment: str | None = None,
                     owner: str | None = None, checkers: list[str] | None = None,
                     refs: list[str] | None = None, paths: list[str] | None = None,
                     source: str | None = None, confidence: str | None = None,
                     blocking: bool = False, at: str | None = None) -> dict[str, Any]:
    item_kind = validate_kind(kind)
    evt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_RECORD,
        "run_id": validate_run_id(run_id),
        "kind": item_kind,
        "key": validate_key(key),
        "status": validate_status(item_kind, status),
        "summary": _bounded_text(summary, "summary", max_bytes=SUMMARY_MAX_BYTES),
        "actor": validate_agent(actor),
        "blocking": bool(blocking),
        "updated_at": at or utc_now(),
    }
    if segment:
        evt["segment"] = validate_key(segment)
    if owner:
        evt["owner"] = validate_agent(owner, field="owner")
    checker_list = _agent_list(checkers, "checker")
    if checker_list:
        evt["checkers"] = checker_list
    ref_list = _bounded_list(refs, "ref", max_bytes=REF_MAX_BYTES)
    if ref_list:
        evt["refs"] = ref_list
    path_list = _path_list(paths)
    if path_list:
        evt["paths"] = path_list
    if source:
        if source not in CLAIM_SOURCES:
            raise OnboardingError(
                f"source must be one of {sorted(CLAIM_SOURCES)}, got {source!r}")
        evt["source"] = source
    if confidence:
        if confidence not in CONFIDENCE_LEVELS:
            raise OnboardingError(
                f"confidence must be one of {sorted(CONFIDENCE_LEVELS)}, got {confidence!r}")
        evt["confidence"] = confidence
    return evt


def event_problem(evt: object) -> str | None:
    if not isinstance(evt, dict):
        return "not a JSON object"
    if evt.get("schema_version") != SCHEMA_VERSION:
        return "schema_version must be 1"
    event = evt.get("event")
    try:
        if event == EVENT_CREATE:
            new_create_event(
                run_id=evt.get("run_id"),
                title=evt.get("title"),
                objective=evt.get("objective"),
                base_ref=evt.get("base_ref"),
                lead=evt.get("lead"),
                state=evt.get("state"),
                at=_bounded_text(evt.get("created_at"), "created_at", max_bytes=80),
            )
            _bounded_text(evt.get("updated_at"), "updated_at", max_bytes=80)
        elif event == EVENT_STATE:
            new_state_event(
                run_id=evt.get("run_id"),
                state=evt.get("state"),
                actor=evt.get("actor"),
                summary=evt.get("summary"),
                at=_bounded_text(evt.get("updated_at"), "updated_at", max_bytes=80),
            )
        elif event == EVENT_RECORD:
            new_record_event(
                run_id=evt.get("run_id"),
                kind=evt.get("kind"),
                key=evt.get("key"),
                status=evt.get("status"),
                summary=evt.get("summary"),
                actor=evt.get("actor"),
                segment=evt.get("segment"),
                owner=evt.get("owner"),
                checkers=evt.get("checkers"),
                refs=evt.get("refs"),
                paths=evt.get("paths"),
                source=evt.get("source"),
                confidence=evt.get("confidence"),
                blocking=bool(evt.get("blocking")),
                at=_bounded_text(evt.get("updated_at"), "updated_at", max_bytes=80),
            )
        else:
            return "event must be create|state|record"
    except OnboardingError as e:
        return str(e)
    return None


def onboarding_dir(store) -> Any:
    return store.dir / STORE_DIRNAME


def run_dir(store, run_id: str) -> Any:
    return onboarding_dir(store) / validate_run_id(run_id)


def events_path(store, run_id: str) -> Any:
    return run_dir(store, run_id) / EVENTS_FILENAME


def _fsync_parent(path: Any) -> None:
    try:
        dfd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_event_locked(store, event: dict[str, Any]) -> None:
    problem = event_problem(event)
    if problem is not None:
        raise OnboardingError(problem)
    path = events_path(store, event["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_parent(path.parent)


def append_event(store, event: dict[str, Any]) -> None:
    with store._config_lock():
        write_event_locked(store, event)


def read_events(store, run_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = events_path(store, run_id)
    if not path.exists():
        return [], []
    valid: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return [], [{"line": 0, "error": f"unreadable: {e}"}]
    for n, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError as e:
            problems.append({"line": n, "error": f"invalid json: {e}"})
            continue
        if evt.get("run_id") != run_id:
            problems.append({"line": n, "error": "run_id does not match directory"})
            continue
        problem = event_problem(evt)
        if problem is not None:
            problems.append({"line": n, "error": problem})
            continue
        valid.append(evt)
    return valid, problems


def _record_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("segment") or ""), str(item.get("key") or ""))


def run_view(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    base: dict[str, Any] | None = None
    records: dict[tuple[str, str], dict[str, Any]] = {}
    state_history: list[dict[str, Any]] = []
    for evt in events:
        if event_problem(evt) is not None:
            continue
        if evt.get("event") == EVENT_CREATE:
            base = {
                "id": evt["run_id"],
                "run_id": evt["run_id"],
                "title": evt.get("title", ""),
                "objective": evt.get("objective", ""),
                "base_ref": evt.get("base_ref", ""),
                "lead": evt.get("lead", ""),
                "state": evt.get("state", "scanning"),
                "created_at": evt.get("created_at", ""),
                "updated_at": evt.get("updated_at", ""),
                "state_summary": "",
            }
            state_history = [{
                "state": base["state"],
                "actor": base["lead"],
                "summary": "",
                "updated_at": base["updated_at"],
            }]
            records = {}
            continue
        if base is None:
            continue
        if evt.get("event") == EVENT_STATE:
            base["state"] = evt.get("state", base.get("state"))
            base["updated_at"] = evt.get("updated_at", base.get("updated_at", ""))
            base["state_summary"] = evt.get("summary", "")
            state_history.append({
                "state": evt.get("state", ""),
                "actor": evt.get("actor", ""),
                "summary": evt.get("summary", ""),
                "updated_at": evt.get("updated_at", ""),
            })
        elif evt.get("event") == EVENT_RECORD:
            records[(evt["kind"], evt["key"])] = dict(evt)
            if evt.get("updated_at", "") >= base.get("updated_at", ""):
                base["updated_at"] = evt.get("updated_at", "")
    if base is None:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in sorted(ITEM_KINDS)}
    for rec in records.values():
        grouped.setdefault(rec["kind"], []).append(rec)
    for rows in grouped.values():
        rows.sort(key=_record_sort_key)
    base["records"] = grouped
    base["state_history"] = state_history
    base["counts"] = counts_for_records(grouped)
    base["blocked"] = (
        base["state"] == "blocked"
        or bool(base["counts"]["blocking_unknowns"])
        or bool(base["counts"]["needs_human_claims"])
        or bool(base["counts"]["blocking_records"])
    )
    base["active"] = base["state"] not in TERMINAL_RUN_STATES
    return base


def counts_for_records(records: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    segments = records.get(KIND_SEGMENT, [])
    claims = records.get(KIND_CLAIM, [])
    drift = records.get(KIND_DRIFT, [])
    unknowns = records.get(KIND_UNKNOWN, [])
    all_records = segments + claims + drift + unknowns
    needs_human_claims = sum(1 for r in claims if r.get("status") == "needs-human")
    blocking_records = sum(1 for r in all_records if bool(r.get("blocking")))
    human_needed = sum(
        1 for r in all_records
        if bool(r.get("blocking")) or (r.get("kind") == KIND_CLAIM and r.get("status") == "needs-human")
    )
    return {
        "segments": len(segments),
        "accepted_segments": sum(1 for r in segments if r.get("status") == "accepted"),
        "blocked_segments": sum(1 for r in segments if r.get("status") == "blocked"),
        "claims": len(claims),
        "confirmed_claims": sum(1 for r in claims if r.get("status") == "confirmed"),
        "conflicted_claims": sum(1 for r in claims if r.get("status") == "conflicted"),
        "needs_human_claims": needs_human_claims,
        "drift": len(drift),
        "open_drift": sum(
            1 for r in drift
            if r.get("status") not in ("resolved", "intentional", "deferred")
        ),
        "resolved_drift": sum(
            1 for r in drift
            if r.get("status") in ("resolved", "intentional", "deferred")
        ),
        "unknowns": len(unknowns),
        "open_unknowns": sum(1 for r in unknowns if r.get("status") == "open"),
        "blocking_unknowns": sum(
            1 for r in unknowns if r.get("status") == "open" and bool(r.get("blocking"))
        ),
        "blocking_records": blocking_records,
        "human_needed": human_needed,
    }


def list_run_ids(store) -> list[str]:
    base = onboarding_dir(store)
    if not base.exists():
        return []
    out: list[str] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        try:
            out.append(validate_run_id(child.name))
        except OnboardingError:
            continue
    return sorted(set(out))


def get_run(store, run_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    events, problems = read_events(store, run_id)
    view = run_view(events)
    if view is not None:
        view["problems"] = problems
    return view, problems


def list_runs(store, *, limit: int | None = None) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for run_id in list_run_ids(store):
        view, run_problems = get_run(store, run_id)
        if view is None:
            if run_problems:
                problems.append({"run_id": run_id, "problems": run_problems[:10]})
            continue
        if run_problems:
            view["problems"] = run_problems[:10]
            problems.append({"run_id": run_id, "problems": run_problems[:10]})
        runs.append(view)
    runs.sort(key=lambda r: (r.get("updated_at") or "", r.get("id") or ""), reverse=True)
    total = len(runs)
    truncated = 0
    if limit is not None and limit >= 0 and total > limit:
        truncated = total - limit
        runs = runs[:limit]
    return {
        "runs": runs,
        "total": total,
        "truncated": truncated,
        "problems": problems,
    }
