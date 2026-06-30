"""Wrapper-side advisory health writer."""

from __future__ import annotations

import time
from typing import Any

from agenttalk import health as health_model

from .events import Event, EventType
from .loop import CLASS_AMBIGUOUS, CLASS_INFRA, CLASS_POISON


class WrapperHealthWriter:
    """Best-effort writer for ``state/<agent>.health.json``.

    It records only bounded labels, timestamps, and safe ids. Free-form message
    content, prompts, model output, tool commands, and tool output are not part of
    the schema and are never accepted by this writer.
    """

    def __init__(
        self,
        store,
        agent: str,
        cli: str,
        *,
        mode: str,
        min_interval: float = 5.0,
    ) -> None:
        self.store = store
        self.agent = agent
        self.cli = cli
        self.mode = mode
        self.min_interval = max(0.0, float(min_interval))
        self._state: str | None = None
        self._since: str | None = None
        self._last_progress_at: str | None = None
        self._last_write_mono: float | None = None
        self._request_id: str | None = None
        self._msg_id: str | None = None

    @property
    def state(self) -> str | None:
        return self._state

    def _write(
        self,
        state: str,
        *,
        reason_code: str | None = None,
        progress: bool = False,
        request_id: str | None = None,
        msg_id: str | None = None,
        warnings: list[str] | None = None,
        force: bool = False,
    ) -> None:
        now_mono = time.monotonic()
        state_changed = state != self._state
        if (
            not force
            and not state_changed
            and self._last_write_mono is not None
            and now_mono - self._last_write_mono < self.min_interval
        ):
            return
        now = health_model.now_iso()
        if state_changed or self._since is None:
            self._since = now
            self._state = state
        if progress:
            self._last_progress_at = now
        if request_id is not None:
            self._request_id = request_id
        if msg_id is not None:
            self._msg_id = msg_id
        snap = health_model.build_snapshot(
            agent=self.agent,
            cli=self.cli,
            mode=self.mode,
            state=state,
            updated_at=now,
            since=self._since,
            last_progress_at=self._last_progress_at,
            request_id=self._request_id,
            msg_id=self._msg_id,
            reason_code=reason_code,
            source="wrapper",
            warnings=warnings or [],
        )
        try:
            self.store.write_health(self.agent, snap)
            self._last_write_mono = now_mono
        except Exception:  # noqa: BLE001 - advisory health must not stop the wrapper
            return

    def idle(self, *, reason_code: str = "idle_waiting") -> None:
        self._request_id = None
        self._msg_id = None
        self._write(health_model.STATE_IDLE_WAITING, reason_code=reason_code, force=True)

    def turn_start(self, record: dict[str, Any] | None) -> None:
        record = record if isinstance(record, dict) else {}
        request_id = record.get("request_id")
        msg_id = record.get("id")
        self._request_id = request_id if isinstance(request_id, str) else None
        self._msg_id = msg_id if isinstance(msg_id, str) else None
        self._write(
            health_model.STATE_WORKING_SILENT,
            reason_code="turn_spawned",
            request_id=self._request_id,
            msg_id=self._msg_id,
            force=True,
        )

    def event(self, event: Event) -> None:
        if event.type == EventType.ADAPTER_ERROR and event.retryable:
            reason = "adapter_rate_limit" if _looks_rate_limited(event.text) else "adapter_retryable_error"
            self._write(
                health_model.STATE_RATE_LIMITED_OR_OUTAGE,
                reason_code=reason,
                force=True,
            )
            return
        if event.is_progress:
            self._write(
                health_model.STATE_WORKING_TURN,
                reason_code="progress_event",
                progress=True,
            )

    def degraded(self, signal: object) -> None:
        level = getattr(signal, "level", None)
        reason = "degraded_output_escalated" if level == "escalate" else "degraded_output_detected"
        self._write(health_model.STATE_DEGRADED_OUTPUT, reason_code=reason, force=True)

    def unknown(self, reason_code: str = "health_classifier_error") -> None:
        self._write(health_model.STATE_UNKNOWN, reason_code=reason_code, force=True)

    def failure(self, sig: dict[str, Any], failure_class: str | None) -> None:
        state, reason = classify_failure(sig, failure_class)
        self._write(state, reason_code=reason, force=True)

    def crashed_or_exited(self, *, reason_code: str = "wrapper_exited") -> None:
        self._write(health_model.STATE_CRASHED_OR_EXITED, reason_code=reason_code, force=True)


def _looks_rate_limited(text: str | None) -> bool:
    t = (text or "").lower()
    return any(token in t for token in ("rate", "429", "too many requests", "quota"))


def classify_failure(sig: dict[str, Any], failure_class: str | None) -> tuple[str, str]:
    """Map turn signals to an advisory state plus safe reason code."""
    if sig.get("watchdog"):
        return health_model.STATE_STUCK_SUSPECTED, "turn_watchdog_fired"
    if sig.get("error"):
        return health_model.STATE_RATE_LIMITED_OR_OUTAGE, "spawn_exec_error"
    rc = sig.get("rc")
    if isinstance(rc, int) and rc != 0 and not sig.get("terminal"):
        return health_model.STATE_CRASHED_OR_EXITED, "child_nonzero_exit"
    if failure_class == CLASS_INFRA:
        if sig.get("retryable"):
            return health_model.STATE_RATE_LIMITED_OR_OUTAGE, "retryable_transport_error"
        return health_model.STATE_RATE_LIMITED_OR_OUTAGE, "terminal_infra_error"
    if failure_class == CLASS_POISON:
        return health_model.STATE_ERRORED_POISON, "poison_eligible_failure"
    if failure_class == CLASS_AMBIGUOUS:
        if sig.get("started") and not sig.get("completed"):
            return health_model.STATE_ERRORED_AMBIGUOUS, "partial_stream"
        return health_model.STATE_ERRORED_AMBIGUOUS, "ambiguous_failure"
    return health_model.STATE_UNKNOWN, "health_classifier_error"
