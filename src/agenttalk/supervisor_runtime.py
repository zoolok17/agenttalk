"""Strict, field-owned observations about the external supervisor runtime."""

from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk import supervisor_lifecycle
from agenttalk.store import Store


RUNTIME_OBSERVATION_SCHEMA = 1
RUNTIME_OBSERVATION_FILENAME = "supervisor-runtime-observation.json"
RUNTIME_OBSERVATION_MAX_BYTES = 16_384
KILL_SWITCH_PHASES = frozenset({"startup", "mid_poll"})

_RUNTIME_KIND = "supervisor_runtime_observation"
_ACTIVATION_ID_RE = re.compile(r"[0-9a-f]{32}")
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "kind", "project_id", "kill_switch"})
_KILL_SWITCH_FIELDS = frozenset({
    "active",
    "activation_id",
    "first_observed_at",
    "first_observed_at_epoch",
    "observations",
    "resolved_at",
    "resolved_at_epoch",
})
_PHASE_FIELDS = frozenset({
    "observed_at",
    "observed_at_epoch",
    "observer_pid",
    "observer_pid_start",
    "exit_code",
})


class SupervisorRuntimeObservationError(RuntimeError):
    """A checked supervisor runtime observation could not be trusted or written."""


def runtime_observation_path(store: Store) -> Path:
    return store.state_dir / RUNTIME_OBSERVATION_FILENAME


def _utc_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _require_epoch(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SupervisorRuntimeObservationError(f"{field} must be a finite epoch")
    try:
        epoch = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise SupervisorRuntimeObservationError(
            f"{field} must be a finite epoch"
        ) from exc
    if not math.isfinite(epoch):
        raise SupervisorRuntimeObservationError(f"{field} must be a finite epoch")
    return epoch


def _require_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise SupervisorRuntimeObservationError(f"{field} must be a bounded timestamp")
    return value


def _validate_phase_observation(phase: str, value: object) -> dict:
    if not isinstance(value, dict) or not set(value).issubset(_PHASE_FIELDS):
        raise SupervisorRuntimeObservationError(
            f"kill_switch.observations.{phase} has an invalid shape"
        )
    required = _PHASE_FIELDS if phase == "startup" else _PHASE_FIELDS - {"exit_code"}
    if set(value) != required:
        raise SupervisorRuntimeObservationError(
            f"kill_switch.observations.{phase} has missing or extra fields"
        )
    observed_at = _require_timestamp(
        value.get("observed_at"), f"kill_switch.observations.{phase}.observed_at"
    )
    observed_at_epoch = _require_epoch(
        value.get("observed_at_epoch"),
        f"kill_switch.observations.{phase}.observed_at_epoch",
    )
    observer_pid = value.get("observer_pid")
    if (
        not isinstance(observer_pid, int)
        or isinstance(observer_pid, bool)
        or observer_pid <= 0
    ):
        raise SupervisorRuntimeObservationError(
            f"kill_switch.observations.{phase}.observer_pid must be positive"
        )
    observer_pid_start = value.get("observer_pid_start")
    if observer_pid_start is not None and (
        not isinstance(observer_pid_start, str) or len(observer_pid_start) > 256
    ):
        raise SupervisorRuntimeObservationError(
            f"kill_switch.observations.{phase}.observer_pid_start is invalid"
        )
    clean = {
        "observed_at": observed_at,
        "observed_at_epoch": observed_at_epoch,
        "observer_pid": observer_pid,
        "observer_pid_start": observer_pid_start,
    }
    if phase == "startup":
        if value.get("exit_code") != 3:
            raise SupervisorRuntimeObservationError(
                "kill_switch.observations.startup.exit_code must be 3"
            )
        clean["exit_code"] = 3
    return clean


def _validate_record(store: Store, value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL_FIELDS:
        raise SupervisorRuntimeObservationError(
            "runtime observation has missing or unknown top-level fields"
        )
    if value.get("schema_version") != RUNTIME_OBSERVATION_SCHEMA:
        raise SupervisorRuntimeObservationError("runtime observation schema is unsupported")
    if value.get("kind") != _RUNTIME_KIND:
        raise SupervisorRuntimeObservationError("runtime observation kind is invalid")
    if value.get("project_id") != store.project_id():
        raise SupervisorRuntimeObservationError(
            "runtime observation belongs to a different project"
        )
    kill_switch = value.get("kill_switch")
    if not isinstance(kill_switch, dict) or not set(kill_switch).issubset(
        _KILL_SWITCH_FIELDS
    ):
        raise SupervisorRuntimeObservationError(
            "runtime observation kill_switch field is invalid"
        )
    required = _KILL_SWITCH_FIELDS - {"resolved_at", "resolved_at_epoch"}
    active = kill_switch.get("active")
    if active is False:
        required = _KILL_SWITCH_FIELDS
    if not isinstance(active, bool) or set(kill_switch) != required:
        raise SupervisorRuntimeObservationError(
            "runtime observation kill_switch fields do not match its active state"
        )
    activation_id = kill_switch.get("activation_id")
    if not isinstance(activation_id, str) or not _ACTIVATION_ID_RE.fullmatch(
        activation_id
    ):
        raise SupervisorRuntimeObservationError("kill_switch.activation_id is invalid")
    first_observed_at = _require_timestamp(
        kill_switch.get("first_observed_at"), "kill_switch.first_observed_at"
    )
    first_observed_at_epoch = _require_epoch(
        kill_switch.get("first_observed_at_epoch"),
        "kill_switch.first_observed_at_epoch",
    )
    observations = kill_switch.get("observations")
    if (
        not isinstance(observations, dict)
        or not observations
        or not set(observations).issubset(KILL_SWITCH_PHASES)
    ):
        raise SupervisorRuntimeObservationError(
            "kill_switch.observations must contain known phases"
        )
    clean_observations = {
        phase: _validate_phase_observation(phase, observation)
        for phase, observation in observations.items()
    }
    clean_kill_switch = {
        "active": active,
        "activation_id": activation_id,
        "first_observed_at": first_observed_at,
        "first_observed_at_epoch": first_observed_at_epoch,
        "observations": clean_observations,
    }
    if active is False:
        clean_kill_switch["resolved_at"] = _require_timestamp(
            kill_switch.get("resolved_at"), "kill_switch.resolved_at"
        )
        clean_kill_switch["resolved_at_epoch"] = _require_epoch(
            kill_switch.get("resolved_at_epoch"), "kill_switch.resolved_at_epoch"
        )
    return {
        "schema_version": RUNTIME_OBSERVATION_SCHEMA,
        "kind": _RUNTIME_KIND,
        "project_id": store.project_id(),
        "kill_switch": clean_kill_switch,
    }


def _read_strict(store: Store) -> dict | None:
    path = runtime_observation_path(store)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SupervisorRuntimeObservationError(
            f"runtime observation is unreadable: {type(exc).__name__}"
        ) from exc
    if size > RUNTIME_OBSERVATION_MAX_BYTES:
        raise SupervisorRuntimeObservationError("runtime observation exceeds its size cap")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, RecursionError, ValueError) as exc:
        raise SupervisorRuntimeObservationError(
            f"runtime observation is malformed: {type(exc).__name__}"
        ) from exc
    return _validate_record(store, value)


def read_runtime_observation(
    store: Store,
) -> tuple[dict | None, list[str]]:
    """Read the latest strict record without turning corruption into evidence."""
    try:
        return _read_strict(store), []
    except SupervisorRuntimeObservationError as exc:
        return None, [f"supervisor_runtime_observation_invalid:{exc}"]


def _phase_observation(
    phase: str,
    *,
    observer_pid: int,
    observer_pid_start: str | None,
    now_epoch: float,
) -> dict:
    value = {
        "observed_at": _utc_text(now_epoch),
        "observed_at_epoch": now_epoch,
        "observer_pid": observer_pid,
        "observer_pid_start": observer_pid_start,
    }
    if phase == "startup":
        value["exit_code"] = 3
    return value


def _new_active_record(
    store: Store,
    *,
    phase: str,
    observer_pid: int,
    observer_pid_start: str | None,
    now_epoch: float,
) -> dict:
    observed = _phase_observation(
        phase,
        observer_pid=observer_pid,
        observer_pid_start=observer_pid_start,
        now_epoch=now_epoch,
    )
    return {
        "schema_version": RUNTIME_OBSERVATION_SCHEMA,
        "kind": _RUNTIME_KIND,
        "project_id": store.project_id(),
        "kill_switch": {
            "active": True,
            "activation_id": uuid.uuid4().hex,
            "first_observed_at": observed["observed_at"],
            "first_observed_at_epoch": observed["observed_at_epoch"],
            "observations": {phase: observed},
        },
    }


def observe_powershell_kill_switch(
    store: Store,
    *,
    phase: str,
    observer_pid: int,
    observer_pid_start: object,
    now_epoch: float | None,
    validate_artifacts: Callable[[], None],
) -> dict:
    """Atomically fold one observed kill-switch level without executor authority.

    The durable operation authenticates the generated PowerShell ancestry and
    owns ``lifecycle -> selection -> config`` while rechecking the raw switch.
    It never reads or claims the executor marker/token.
    """
    if phase not in KILL_SWITCH_PHASES:
        raise ValueError(f"unsupported kill-switch observation phase: {phase!r}")
    if (
        not isinstance(observer_pid, int)
        or isinstance(observer_pid, bool)
        or observer_pid <= 0
    ):
        raise ValueError("observer_pid must be a positive integer")
    if observer_pid_start is not None and (
        not isinstance(observer_pid_start, str) or len(observer_pid_start) > 256
    ):
        raise ValueError("observer_pid_start must be a bounded string or null")
    observed_epoch = _require_epoch(
        time.time() if now_epoch is None else now_epoch,
        "now_epoch",
    )

    with supervisor_lifecycle.checked_powershell_supervisor_observer(
        store,
        pid=observer_pid,
        pid_start=observer_pid_start,
        validate_artifacts=validate_artifacts,
    ):
        changed = False
        active = store.supervisor_kill_switch()
        if active is None:
            raise SupervisorRuntimeObservationError(
                "supervisor.kill state is unreadable"
            )
        try:
            record = _read_strict(store)
        except SupervisorRuntimeObservationError:
            # This sidecar is evidence, never authority.  Preserve malformed or
            # future-schema bytes for diagnosis, but do not let them keep a
            # supervisor disabled after the raw emergency switch is absent.
            if active:
                raise
            record = None
        if active:
            if record is None or record["kill_switch"]["active"] is False:
                record = _new_active_record(
                    store,
                    phase=phase,
                    observer_pid=observer_pid,
                    observer_pid_start=observer_pid_start,
                    now_epoch=observed_epoch,
                )
                changed = True
            elif phase not in record["kill_switch"]["observations"]:
                record = copy.deepcopy(record)
                record["kill_switch"]["observations"][phase] = _phase_observation(
                    phase,
                    observer_pid=observer_pid,
                    observer_pid_start=observer_pid_start,
                    now_epoch=observed_epoch,
                )
                changed = True
        elif record is not None and record["kill_switch"]["active"] is True:
            record = copy.deepcopy(record)
            record["kill_switch"]["active"] = False
            record["kill_switch"]["resolved_at"] = _utc_text(observed_epoch)
            record["kill_switch"]["resolved_at_epoch"] = observed_epoch
            changed = True
        if changed:
            _atomic_write_text(
                runtime_observation_path(store),
                json.dumps(
                    _validate_record(store, record),
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n",
            )
    return {
        "active": bool(active),
        "changed": changed,
        "observation": copy.deepcopy(record),
    }


def build_runtime_status(store: Store) -> dict:
    """Build the human/JSON status projection independently of event history."""
    active = store.supervisor_kill_switch()
    record, warnings = read_runtime_observation(store)
    result: dict[str, object] = {
        "active": active,
        "observed": False,
        "path": str(runtime_observation_path(store)),
    }
    if active is None:
        warnings.insert(0, "supervisor_kill_switch_unreadable")
    if record is not None:
        kill_switch = record["kill_switch"]
        result.update({
            "activation_id": kill_switch["activation_id"],
            "first_observed_at": kill_switch["first_observed_at"],
            "first_observed_at_epoch": kill_switch["first_observed_at_epoch"],
            "observations": copy.deepcopy(kill_switch["observations"]),
            "record_active": kill_switch["active"],
        })
        if "resolved_at" in kill_switch:
            result["resolved_at"] = kill_switch["resolved_at"]
            result["resolved_at_epoch"] = kill_switch["resolved_at_epoch"]
        result["observed"] = active is True and kill_switch["active"] is True
    if warnings:
        result["warnings"] = warnings
    return {"kill_switch": result}


def runtime_status_relevant(value: object) -> bool:
    """Whether an additive status projection carries non-baseline evidence."""
    if not isinstance(value, dict):
        return False
    kill_switch = value.get("kill_switch")
    if not isinstance(kill_switch, dict):
        return False
    return (
        kill_switch.get("active") is not False
        or "activation_id" in kill_switch
        or bool(kill_switch.get("warnings"))
    )
