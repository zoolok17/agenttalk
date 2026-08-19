"""Lightweight assurance gates and review-result evidence validation."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from agenttalk._atomic import write_text as _atomic_write_text

SCHEMA_VERSION = 1

VALID_STATUSES = frozenset({"red", "green", "unknown", "skipped", "waived"})
VALID_SEVERITIES = frozenset({"blocker", "warn", "info"})
VALID_EVIDENCE_SOURCES = frozenset({"automation_ci", "local_command", "manual_review", "operator_waiver"})
BLOCKER_GREEN_SOURCES = frozenset({"automation_ci", "operator_waiver"})
VALID_RELEASE_BLOCKERS = frozenset({"yes", "no", "unknown"})
CORE_RISK_CLASSES = frozenset({
    "none",
    "unknown",
    "release",
    "device",
    "accessibility",
    "security",
    "performance",
    "persistence",
    "docs-contract",
    "quality",
})
RESPONSE_STATUS_ENUMS = {
    "review-result": frozenset({"approved", "rejected", "needs-info"}),
    "proposal-response": frozenset({"accepted", "rejected", "countered"}),
}
TERMINAL_RESPONSE_STATUSES = {
    "review-result": frozenset({"approved", "rejected"}),
    "proposal-response": RESPONSE_STATUS_ENUMS["proposal-response"],
}

_SAFE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RISK_EXTENSION_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_NA_VALUES = frozenset({"na", "n/a", "none", "not-applicable", "not applicable"})


def gates_path(root: Path) -> Path:
    return Path(root).resolve() / ".agenttalk" / "gates.json"


def load_gate_state(root: Path) -> dict:
    path = gates_path(root)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "required_gates": [], "gates": {}}
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_members,
        )
    except (json.JSONDecodeError, OSError, ValueError) as e:
        return _state_load_error(f"could not read gate state: {e}")
    try:
        return _normalize_loaded_state(data)
    except ValueError as e:
        return _state_load_error(str(e))


def write_gate_state(root: Path, state: dict) -> None:
    state = {
        "schema_version": SCHEMA_VERSION,
        "required_gates": sorted(set(state.get("required_gates") or [])),
        "gates": state.get("gates") or {},
    }
    _atomic_write_text(gates_path(root), json.dumps(state, indent=2, ensure_ascii=False))


def set_gate(
    root: Path,
    *,
    name: str,
    status: str,
    severity: str,
    scope: str,
    actor: str,
    evidence_source: str,
    evidence: list[str] | None = None,
    evidence_details: dict[str, Any] | None = None,
    reason: str | None = None,
    revision: str | None = None,
    epoch: str | None = None,
    required: bool | None = None,
) -> dict:
    _validate_gate_name(name)
    status = _validate_choice("status", status, VALID_STATUSES)
    severity = _validate_choice("severity", severity, VALID_SEVERITIES)
    evidence_source = _validate_choice("evidence_source", evidence_source, VALID_EVIDENCE_SOURCES)
    scope = _clean_text("scope", scope)
    if status == "waived" or evidence_source == "operator_waiver":
        raise ValueError("operator_waiver decisions must be recorded with `agenttalk gate waive`")
    if status == "green" and severity == "blocker" and evidence_source not in BLOCKER_GREEN_SOURCES:
        raise ValueError(
            "severity=blocker gates may be set green only from source=automation_ci "
            "or through `agenttalk gate waive` for operator waivers"
        )
    if status == "green" and severity == "blocker" and not evidence:
        raise ValueError("severity=blocker gates need evidence before they can be set green")
    if evidence_details is not None:
        if not isinstance(evidence_details, dict):
            raise ValueError("evidence_details must be an object")
        reserved = {"source", "refs", "at", "by"}.intersection(evidence_details)
        if reserved:
            raise ValueError(f"evidence_details cannot replace reserved fields: {sorted(reserved)}")
        if "coverage_percent" in evidence_details:
            coverage_percent = evidence_details["coverage_percent"]
            if (
                type(coverage_percent) is not float
                or not math.isfinite(coverage_percent)
                or not 0.0 <= coverage_percent <= 100.0
            ):
                raise ValueError("evidence_details.coverage_percent must be a finite float in [0, 100]")
        try:
            json.dumps(evidence_details, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence_details must contain finite JSON values") from exc
    state = load_gate_state(root)
    _refuse_on_load_error(state)
    gate = dict(state["gates"].get(name, {}))
    now = _now_iso()
    gate.update({
        "name": name,
        "status": status,
        "severity": severity,
        "scope": scope,
        "updated_by": actor,
        "updated_at": now,
        "evidence_source": evidence_source,
    })
    if reason is not None:
        gate["reason"] = reason
    if revision is not None:
        gate["revision"] = revision
    if epoch is not None:
        gate["epoch"] = epoch
    if evidence or evidence_details:
        entries = gate.get("evidence")
        if not isinstance(entries, list):
            entries = []
        entry = {
            "source": evidence_source,
            "refs": list(evidence or []),
            "at": now,
            "by": actor,
        }
        if evidence_details:
            entry.update(evidence_details)
        entries.append(entry)
        gate["evidence"] = entries
    state["gates"][name] = gate
    required_set = set(state["required_gates"])
    if required is True:
        required_set.add(name)
    elif required is False:
        required_set.discard(name)
    state["required_gates"] = sorted(required_set)
    write_gate_state(root, state)
    return gate


def waive_gate(
    root: Path,
    *,
    name: str,
    operator: str,
    reason: str,
    scope: str,
    expires: str,
    date: str | None = None,
    actor: str | None = None,
) -> dict:
    _validate_gate_name(name)
    operator = _clean_text("operator", operator)
    reason = _clean_text("reason", reason)
    scope = _clean_text("scope", scope)
    _parse_expiry(expires)
    state = load_gate_state(root)
    _refuse_on_load_error(state)
    now = _now_iso()
    gate = dict(state["gates"].get(name, {"name": name}))
    gate.update({
        "name": name,
        "status": "waived",
        "severity": gate.get("severity", "blocker"),
        "scope": scope,
        "updated_by": actor or operator,
        "updated_at": now,
        "evidence_source": "operator_waiver",
        "waiver": {
            "operator": operator,
            "date": date or now,
            "reason": reason,
            "scope": scope,
            "expires": expires,
        },
    })
    state["gates"][name] = gate
    write_gate_state(root, state)
    return gate


def check_gates(root: Path, *, scope: str | None = None, now: datetime | None = None) -> dict:
    state = load_gate_state(root)
    gates = state["gates"]
    required = sorted(set(state["required_gates"]))
    now = now or datetime.now(timezone.utc)
    checked: list[dict] = []
    blockers: list[dict] = []
    if state.get("load_error"):
        item = {
            "name": "__gate_state__",
            "status": "unknown",
            "severity": "blocker",
            "scope": scope or "global",
            "blocks": True,
            "reason": str(state["load_error"]),
        }
        checked.append(item)
        blockers.append(item)
    for name in required:
        gate = gates.get(name)
        if gate is None:
            item = {
                "name": name,
                "status": "unknown",
                "severity": "blocker",
                "scope": scope or "release",
                "blocks": True,
                "reason": "required gate is missing",
            }
            checked.append(item)
            blockers.append(item)
        elif scope and gate.get("scope") not in (scope, "global"):
            # Present but recorded under a non-matching scope: the scope filter
            # below would skip it, so it must NOT pass the missing-check by
            # presence and then vanish (that was a fail-closed violation -> a
            # false GO). A required gate only satisfies a scoped check when it
            # is applicable to that scope. Block explicitly.
            item = {
                "name": name,
                "status": "unknown",
                "severity": "blocker",
                "scope": scope,
                "blocks": True,
                "reason": (
                    f"required gate recorded under scope {gate.get('scope')!r}, "
                    f"not applicable to checked scope {scope!r}"
                ),
            }
            checked.append(item)
            blockers.append(item)
        # else: present and applicable -> the main loop below evaluates it.
    for _name, gate in sorted(gates.items()):
        if scope and gate.get("scope") not in (scope, "global"):
            continue
        item = _gate_verdict(gate, now=now)
        checked.append(item)
        if item["blocks"]:
            blockers.append(item)
    verdict = "GO" if not blockers else "HOLD"
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "required_gates": required,
        "blockers": blockers,
        "gates": checked,
    }


def validate_response_status(kind: str, meta: dict) -> None:
    """Validate a typed response status when present.

    Missing status remains readable for mixed-version history, but it is not a
    terminal thread event. A present status must be an exact member of the
    response kind's enum.
    """
    allowed = RESPONSE_STATUS_ENUMS.get(kind)
    if allowed is None or "status" not in meta:
        return
    status = meta["status"]
    if not isinstance(status, str) or status not in allowed:
        raise ValueError(
            f"{kind} status must be one of {sorted(allowed)}, got {status!r}"
        )


def validate_review_result_evidence(kind: str, meta: dict[str, Any]) -> None:
    if kind != "review-result" or meta.get("status") != "approved":
        return
    errors: list[str] = []
    required = ["risk_class", "release_blocker", "tests_referenced", "tests_executed", "residual_risk"]
    for key in required:
        if not _has_value(meta.get(key)):
            errors.append(f"missing {key}")
    if not (_has_value(meta.get("evidence")) or _has_value(meta.get("artifacts"))):
        errors.append("missing evidence or artifacts")
    risk_class = str(meta.get("risk_class", "")).strip()
    if risk_class and not is_valid_risk_class(risk_class):
        errors.append(
            "risk_class must be one of the core classes or a namespaced extension like project:name"
        )
    release_blocker = str(meta.get("release_blocker", "")).strip()
    if release_blocker and release_blocker not in VALID_RELEASE_BLOCKERS:
        errors.append("release_blocker must be yes, no, or unknown")
    for key in (*required, "evidence", "artifacts"):
        if _is_na(meta.get(key)) and not _na_reason(meta, key):
            errors.append(f"{key}=n/a needs a na_reason")
    if errors:
        raise ValueError(
            "review-result status=approved needs typed evidence; "
            + "; ".join(errors)
            + ". For a lightweight approval use risk_class=none, release_blocker=no, "
            "n/a evidence fields, and a na_reason."
        )


def is_valid_risk_class(value: object) -> bool:
    """Return whether a risk class is in the shared core or extension envelope."""
    return isinstance(value, str) and (
        value in CORE_RISK_CLASSES or _RISK_EXTENSION_RE.match(value) is not None
    )


def _gate_verdict(gate: dict, *, now: datetime) -> dict:
    status = str(gate.get("status") or "unknown")
    severity = str(gate.get("severity") or "blocker")
    blocks = False
    reason = gate.get("reason") if isinstance(gate.get("reason"), str) else ""
    if status == "waived":
        waiver = gate.get("waiver") if isinstance(gate.get("waiver"), dict) else {}
        if _waiver_active(waiver, now=now):
            blocks = False
        else:
            blocks = severity == "blocker"
            reason = "waiver expired or invalid"
    elif severity == "blocker" and status != "green":
        # A blocker clears ONLY on validated green (load_gate_state enforces the
        # evidence source/refs for a green blocker) or an active waiver (handled
        # above). Every other status blocks - including `skipped`, which means
        # "not run / no usable evidence", NOT "not applicable". Use a waiver for
        # genuine not-applicable, never `skipped`.
        blocks = True
        if not reason:
            if status == "skipped":
                reason = "required evidence not run (skipped); use a waiver for not-applicable"
            elif status == "red":
                reason = "gate is red"
            else:
                reason = f"blocker gate status {status!r} is not a validated green"
    return {
        "name": str(gate.get("name") or ""),
        "status": status,
        "severity": severity,
        "scope": str(gate.get("scope") or "global"),
        "blocks": blocks,
        "reason": reason,
        "updated_at": gate.get("updated_at"),
        "updated_by": gate.get("updated_by"),
        "waiver": gate.get("waiver") if status == "waived" else None,
    }


def _refuse_on_load_error(state: dict) -> None:
    """Fail closed: never overwrite gate state we could not parse.

    A mutation (set/waive) that loaded a corrupt gates.json must NOT silently
    rewrite it with a clean single-gate state - that would erase the existing
    (unparseable) required gates and could flip a later check to a false GO.
    Refuse so the operator repairs or removes the file deliberately. This guard
    lives in the core so direct callers are covered, not only the CLI path.
    """
    if state.get("load_error"):
        raise ValueError(
            f"{state['load_error']} - refusing to overwrite gate state; "
            "repair or remove .agenttalk/gates.json before recording gates"
        )


def _state_load_error(reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "required_gates": [],
        "gates": {},
        "load_error": f"malformed gate state: {reason}",
    }


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _normalize_loaded_state(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("gate state must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported gate state schema_version {data.get('schema_version')!r}"
        )
    required = data.get("required_gates", [])
    if not isinstance(required, list):
        raise ValueError("required_gates must be a list")
    required_gates: list[str] = []
    for name in required:
        _validate_gate_name(name)
        required_gates.append(name)
    gates = data.get("gates", {})
    if not isinstance(gates, dict):
        raise ValueError("gates must be an object")
    normalized_gates: dict[str, dict] = {}
    for name, gate in gates.items():
        _validate_gate_name(name)
        if not isinstance(gate, dict):
            raise ValueError(f"gate {name!r} must be an object")
        normalized_gates[name] = _normalize_loaded_gate(name, gate)
    return {
        "schema_version": SCHEMA_VERSION,
        "required_gates": sorted(set(required_gates)),
        "gates": normalized_gates,
    }


def _normalize_loaded_gate(name: str, gate: dict) -> dict:
    loaded = dict(gate)
    stored_name = loaded.get("name", name)
    if stored_name != name:
        raise ValueError(f"gate {name!r} has mismatched name {stored_name!r}")
    status = loaded.get("status", "unknown")
    severity = loaded.get("severity", "blocker")
    if not isinstance(status, str) or status not in VALID_STATUSES:
        raise ValueError(f"gate {name!r} has invalid status {status!r}")
    if not isinstance(severity, str) or severity not in VALID_SEVERITIES:
        raise ValueError(f"gate {name!r} has invalid severity {severity!r}")
    scope = loaded.get("scope", "global")
    if not isinstance(scope, str) or not scope.strip() or any(ch in scope for ch in "\r\n"):
        raise ValueError(f"gate {name!r} has invalid scope")
    evidence_source = loaded.get("evidence_source")
    if evidence_source is not None and (
        not isinstance(evidence_source, str) or evidence_source not in VALID_EVIDENCE_SOURCES
    ):
        raise ValueError(f"gate {name!r} has invalid evidence_source {evidence_source!r}")
    if status == "green" and severity == "blocker":
        if evidence_source not in BLOCKER_GREEN_SOURCES:
            raise ValueError(f"gate {name!r} green blocker has invalid evidence source")
        if evidence_source == "operator_waiver":
            raise ValueError(f"gate {name!r} operator waiver must use waived status")
        if not _has_gate_evidence(loaded, evidence_source):
            raise ValueError(f"gate {name!r} green blocker is missing evidence")
    loaded["name"] = name
    loaded["status"] = status
    loaded["severity"] = severity
    loaded["scope"] = scope.strip()
    return loaded


def _has_gate_evidence(gate: dict, source: str) -> bool:
    evidence = gate.get("evidence")
    if not isinstance(evidence, list):
        return False
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        refs = entry.get("refs")
        if entry.get("source") == source and isinstance(refs, list) and any(_has_value(ref) for ref in refs):
            return True
    return False


def _validate_gate_name(name: str) -> None:
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"gate name {name!r} is not safe "
            "(allowed: alphanumeric plus . _ : -, must start alphanumeric, max 128 chars)"
        )


def _validate_choice(label: str, value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{label} must be one of {sorted(allowed)}")
    return value


def _clean_text(label: str, value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    cleaned = value.strip()
    if any(ch in cleaned for ch in "\r\n"):
        raise ValueError(f"{label} must be a single line")
    return cleaned


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_expiry(value: str) -> datetime:
    raw = _clean_text("expires", value)
    if re.match(r"\A\d{4}-\d{2}-\d{2}\Z", raw):
        return datetime.combine(datetime.fromisoformat(raw).date(), time.max, tzinfo=timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _waiver_active(waiver: dict, *, now: datetime) -> bool:
    try:
        expires = _parse_expiry(str(waiver.get("expires", "")))
    except (TypeError, ValueError):
        return False
    return expires >= now and all(_has_value(waiver.get(k)) for k in ("operator", "date", "reason", "scope"))


def _has_value(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_na(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() in _NA_VALUES


def _na_reason(meta: dict[str, Any], key: str) -> str | None:
    norm = key.replace("/", "_").replace("-", "_")
    for candidate in (f"na_reason_{norm}", f"na_reason.{key}", "na_reason"):
        value = meta.get(candidate)
        if isinstance(value, str) and value.strip():
            return value
    return None
