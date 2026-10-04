"""Wrapper-side advisory health writer."""

from __future__ import annotations

import re
import time
from typing import Any

from agenttalk import __version__
from agenttalk import health as health_model
from agenttalk.correlation import resolve_request_id

from . import usage_park
from .events import Event, EventType
from .usage_park import REASON_PARKED as USAGE_LIMIT_PARKED_REASON
from .loop import (
    CLASS_AMBIGUOUS,
    CLASS_CONFIG_BLOCKED,
    CLASS_GATEWAY_HELD,
    CLASS_INFRA,
    CLASS_POISON,
    LOCK_CONTENTION_REASON,
)


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
        # Closed words the wrapper wants on every record it writes anyway (for example
        # that the optional turn journal never started). Set once, never read from input.
        self.standing_warnings: tuple[str, ...] = ()
        self._reason: str | None = None
        # #305 fix round 1, connector 4177637201: the validated, in-turn proof that THIS
        # turn hit a named usage-limit window (set by `event()` from the SAME rate_limit_event
        # proof the park decision trusts) - carried through to `failure()`'s terminal
        # classification, which otherwise independently re-derives a weaker, unrelated
        # terminal reason (a 429/529 on the same stream, or none at all) and silently
        # overwrites the stronger evidence. Scoped to one turn: cleared at the next
        # `turn_start`, never read back across turns.
        self._usage_limit_evidence: tuple[str, str] | None = None

    @property
    def state(self) -> str | None:
        return self._state

    def _write(
        self,
        state: str,
        *,
        reason_code: str | None = None,
        reason_detail: str | None = None,
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
            reason_detail=reason_detail,
            source="wrapper",
            warnings=[*(warnings or []), *self.standing_warnings],
            agenttalk_version=__version__,
        )
        try:
            self.store.write_health(self.agent, snap)
            self._last_write_mono = now_mono
            self._reason = reason_code
        except Exception:  # noqa: BLE001 - advisory health must not stop the wrapper
            return

    def idle(self, *, reason_code: str = "idle_waiting",
             warnings: list[str] | None = None) -> None:
        self._request_id = None
        self._msg_id = None
        if reason_code == "idle_waiting" and self._has_unresolved_reply_refusal():
            # #162: an idle seat sitting on an UNRESOLVED refused reply is not
            # the same signal as a genuinely quiet idle seat - a disk check
            # (not a value threaded synchronously through the turn that
            # produced it) so this catches the refusal on EVERY idle tick
            # that follows, regardless of which internal commit path handled
            # that turn or how many polls separate the two events. Only
            # overrides the DEFAULT reason - an explicit caller-supplied
            # reason_code (e.g. "turn_spawned") is never clobbered.
            reason_code = "reply_refused"
        # #wrapper-reply-channels increment C: `warnings` is the caller's own
        # already-computed stray-reply-draft labels (loop._report_stray_
        # reply_drafts, called once per clean turn right after draft
        # delivery) - unlike the refusal check above, this is NOT re-derived
        # here, since the caller already paid for the disk scan this same
        # tick and a second independent scan would just double the I/O for
        # no new information.
        self._write(health_model.STATE_IDLE_WAITING, reason_code=reason_code,
                    warnings=warnings, force=True)

    def _has_unresolved_reply_refusal(self) -> bool:
        try:
            from agenttalk import reply_refusals
            for item in reply_refusals.list_reply_refusals(self.store, self.agent):
                mid = item.get("original_message_id")
                if isinstance(mid, str) and not reply_refusals.is_resolved(
                        self.store, self.agent, mid):
                    return True
        except Exception:  # noqa: BLE001 - advisory health must not crash the wrapper
            return False
        return False

    def turn_start(self, record: dict[str, Any] | None) -> None:
        record = record if isinstance(record, dict) else {}
        request_id = record.get("request_id")
        msg_id = record.get("id")
        self._request_id = request_id if isinstance(request_id, str) else None
        self._msg_id = msg_id if isinstance(msg_id, str) else None
        self._usage_limit_evidence = None
        self._write(
            health_model.STATE_WORKING_SILENT,
            reason_code="turn_spawned",
            request_id=self._request_id,
            msg_id=self._msg_id,
            force=True,
        )

    def parked(self, record: dict[str, Any] | None, reason_code: str = "config_blocked") -> None:
        """The loop is HOLDING a config-blocked head without driving it (deterministic local
        exec/config denial - e.g. a held gateway, an exec-denied bus write). Surface it as a
        distinct advisory state carrying the parked head's ids, so ``status``/``doctor`` show
        the worker as blocked-on-a-message instead of a frozen last state (the health-freeze
        that hid the wedge, #58). Not forced: the first park is a state change (written at once)
        and repeats throttle, so a long park is one visible transition, not a write storm."""
        record = record if isinstance(record, dict) else {}
        request_id = resolve_request_id(record)
        msg_id = record.get("id")
        if reason_code == USAGE_LIMIT_PARKED_REASON:
            # A head parked on a provider usage limit is an outage-like wait that ends by
            # itself - never a config error. The existing rate_limited_or_outage state, with
            # its own reason. A change of reason is written at once; repeats throttle.
            self._write(
                health_model.STATE_RATE_LIMITED_OR_OUTAGE,
                reason_code=reason_code,
                request_id=request_id if isinstance(request_id, str) else None,
                msg_id=msg_id if isinstance(msg_id, str) else None,
                force=self._reason != reason_code,
            )
            return
        self._write(
            health_model.STATE_ERRORED_AMBIGUOUS,
            reason_code=reason_code,
            request_id=request_id if isinstance(request_id, str) else None,
            msg_id=msg_id if isinstance(msg_id, str) else None,
        )

    def lock_contention(self, phase: str) -> None:
        """#154: the loop is retrying one store step IN PLACE under store lock
        contention - an outage-like, self-healing wait, so the existing
        rate_limited_or_outage state with a specific reason (and the phase as a
        warning label), not idle and not an error. Not forced: the first attempt
        is a state change (written at once) and repeats throttle."""
        self._write(
            health_model.STATE_RATE_LIMITED_OR_OUTAGE,
            reason_code=LOCK_CONTENTION_REASON,
            warnings=[f"lock_contention_{phase}"],
        )

    def event(self, event: Event) -> None:
        if event.type == EventType.ADAPTER_ERROR and event.retryable:
            reason, detail = _classify_adapter_error(self.cli, event)
            if reason == REASON_USAGE_LIMIT_REJECTED and detail is not None:
                self._usage_limit_evidence = (reason, detail)
            self._write(
                health_model.STATE_RATE_LIMITED_OR_OUTAGE,
                reason_code=reason,
                reason_detail=detail,
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
        state, reason, detail = classify_failure(sig, failure_class, self._usage_limit_evidence)
        self._write(state, reason_code=reason, reason_detail=detail, force=True)

    def crashed_or_exited(self, *, reason_code: str = "wrapper_exited") -> None:
        self._write(health_model.STATE_CRASHED_OR_EXITED, reason_code=reason_code, force=True)


REASON_USAGE_LIMIT_REJECTED = "usage_limit_rejected"
REASON_THROTTLED = "throttled"
REASON_OVERLOADED = "overloaded"
REASON_ADAPTER_RATE_LIMIT = "adapter_rate_limit"
REASON_ADAPTER_RETRYABLE_ERROR = "adapter_retryable_error"

# #305: text matching is a LAST resort, on whole phrases only - never the bare substring
# "rate" (it also matched "generate", "separate", "iterate"). Each phrase's value is the
# closed, safe-token label recorded in reason_detail - never the raw matched text.
# Fix round 1, connector 4177637214: a bare substring search still matched a phrase
# INSIDE a larger word ("corporate limit exceeded" contains "rate limit"; "quotable
# statement" contains "quota") - \b on both sides of each phrase requires a real word
# boundary at both ends, not just anywhere in the haystack.
_TEXT_FALLBACK_RATE_LIMIT_MARKERS = (
    (re.compile(r"\brate limit\b", re.IGNORECASE), "rate_limit"),
    (re.compile(r"\btoo many requests\b", re.IGNORECASE), "too_many_requests"),
    (re.compile(r"\bquota\b", re.IGNORECASE), "quota"),
)


def _looks_rate_limited(text: str | None) -> str | None:
    """The safe-token label of the first whole-phrase rate-limit marker found in
    ``text``, or None. Last-resort text matching only - see the module docstring."""
    t = text or ""
    for pattern, label in _TEXT_FALLBACK_RATE_LIMIT_MARKERS:
        if pattern.search(t):
            return label
    return None


def _classify_adapter_error(cli: str, event: Event) -> tuple[str, str | None]:
    """Classify a retryable ADAPTER_ERROR into a ``reason_code`` plus a short, closed-
    vocabulary ``reason_detail`` (#305). Structured evidence first, always:

    1. the SAME proof the usage-limit park decision trusts (a rejected
       ``rate_limit_event`` naming a known window) - never re-derived, reused via
       :func:`usage_park.usage_limit_rejected_window`;
    2. only then, a narrow whole-phrase text match on ``event.text`` (never the bare
       substring "rate");
    3. otherwise the existing, unclassified retryable-error reason.

    A 429/``rate_limit_error`` (throttled) or 529/``overloaded_error`` (overloaded) is
    classified separately, in :func:`classify_failure` - a terminal Claude ``result``
    error is never ``retryable`` (see ``claude_adapter.map_event``), so it never reaches
    this per-stream-event path at all; it reaches the wrapper loop's end-of-turn failure
    classification instead, which already extracts the SAME structured fact this must
    reuse (``sig["structured_errors"]``), never a second parser.

    ``reason_detail`` never carries free text: every value is one of our own closed
    labels, built only from the matched phrase's own fixed name - never the raw
    provider text (see :func:`_infra_reason` for the terminal 429/529 path, which
    applies the same closed-vocabulary rule to a provider-supplied subtype)."""
    raw = event.raw if isinstance(event.raw, dict) else {}
    if cli == "claude" and raw.get("type") == "rate_limit_event":
        window = usage_park.usage_limit_rejected_window(raw.get("rate_limit_info"))
        if window is not None:
            return REASON_USAGE_LIMIT_REJECTED, f"rate_limit_event.rejected.{window}"
    text_label = _looks_rate_limited(event.text)
    if text_label is not None:
        return REASON_ADAPTER_RATE_LIMIT, f"text_match.{text_label}"
    return REASON_ADAPTER_RETRYABLE_ERROR, None


def _setup_failure_reason(sig: dict[str, Any]) -> str | None:
    setup_failure = sig.get("setup_failure")
    if not isinstance(setup_failure, dict):
        return None
    subtype = setup_failure.get("subtype")
    if subtype == "worktree_branch_already_checked_out":
        return "worktree_branch_already_checked_out"
    return None


def _closed_infra_detail(
    status: object, subtype: object, known_status: int, known_subtype: str,
) -> str | None:
    """Fix round 1, connector 4177637208: shape is not privacy. A provider subtype can be
    token-shaped (no spaces, safe characters) and still be private text - a synthetic
    customer id, a path fragment, anything a sanitizer like ``safe_token`` cannot tell
    apart from a real subtype name just by looking at its characters. The only value ever
    recorded is the FIXED constant this reason already matched against (``known_status``/
    ``known_subtype``) - never the raw ``status``/``subtype`` read off the provider, even
    when one of them happens to differ from its known counterpart but still looks safe."""
    parts = []
    if status == known_status:
        parts.append(f"status_{known_status}")
    if subtype == known_subtype:
        parts.append(known_subtype)
    return ".".join(parts) if parts else None


def _infra_reason(sig: dict[str, Any]) -> tuple[str, str | None]:
    """A more specific CLASS_INFRA reason when the turn's OWN structured errors
    (the wrapper loop's ``sig["structured_errors"]``, already extracted by its existing
    Claude error extractor - #305 reuses this, never a second parser) name an HTTP
    429/``rate_limit_error`` (throttled) or 529/``overloaded_error`` (overloaded) terminal
    result. Falls back to the existing generic retryable/terminal wording otherwise -
    unchanged for every older record and every infra cause that is neither of these two."""
    for fact in sig.get("structured_errors") or []:
        if not isinstance(fact, dict) or fact.get("kind") != "result":
            continue
        status = fact.get("api_error_status")
        subtype = fact.get("subtype")
        if status == usage_park.HTTP_STATUS_THROTTLED or subtype == usage_park.SUBTYPE_THROTTLED:
            return REASON_THROTTLED, _closed_infra_detail(
                status, subtype, usage_park.HTTP_STATUS_THROTTLED, usage_park.SUBTYPE_THROTTLED)
        if status == usage_park.HTTP_STATUS_OVERLOADED or subtype == usage_park.SUBTYPE_OVERLOADED:
            return REASON_OVERLOADED, _closed_infra_detail(
                status, subtype, usage_park.HTTP_STATUS_OVERLOADED, usage_park.SUBTYPE_OVERLOADED)
    if sig.get("retryable"):
        return "retryable_transport_error", None
    return "terminal_infra_error", None


def classify_failure(
    sig: dict[str, Any],
    failure_class: str | None,
    usage_limit_evidence: tuple[str, str] | None = None,
) -> tuple[str, str, str | None]:
    """Map turn signals to an advisory state, a safe reason code, and an optional
    closed-vocabulary reason detail (#305).

    ``usage_limit_evidence``, when given, is the ``(reason_code, reason_detail)`` pair an
    EARLIER ``rate_limit_event`` already proved for this same turn (the writer's own
    ``_usage_limit_evidence``, set by ``event()``) - fix round 1, connector 4177637201:
    the terminal classification below used to independently re-derive a weaker, unrelated
    reason (a 429/529 seen later on the same stream, or none at all) and silently overwrite
    that stronger proof. It wins over every terminal cause EXCEPT the two precedence rules
    already in force above it (a fired watchdog, a config-blocked turn) - both of those are
    unrelated to provider rate limiting and must keep deciding the state outright.

    Fix round 2, connector 4177952848 (F7): an earlier rejection is not the LAST word on
    its own - revalidated here against the complete current stream (``sig["usage_stream"]``,
    the same running fold ``usage_park.note_stream_event`` builds turn-long, already read by
    the park decision's own ``_usage_limit_fact``/``fact_from_stream``) before it is allowed
    to override anything. A later terminal result whose ``is_error`` comes back exactly
    ``False`` VETOES it - the provider ultimately let the turn through, whatever else then
    went wrong (here, a nonzero child exit) - and an incomplete stream (no terminal result
    read at all) decides nothing either way: both fall through to the normal rules below,
    same as if no earlier rejection had ever been seen.

    Fix round 1 of #329, connector 4178374147: the stream proof alone is not enough either -
    it only shows the PROVIDER's own side of the stream, never whether something ELSE then
    decided this turn's actual fate. The override is now additionally restricted to an
    ELIGIBLE PROVIDER failure, with the exact same gate ``_usage_limit_fact`` already applies
    for the park decision (``failure_class == CLASS_INFRA`` and no local cause present -
    ``usage_park.local_cause_present``, shared rather than re-derived): a rejected quota
    event followed by an error result AND a failed bus write (or a watchdog, a config
    refusal, a held gateway) is a LOCAL failure, not a provider one - the drive classifier
    already says so (``ambiguous_or_unknown`` or similar), and the retained evidence must
    not override that real decision just because the provider's own earlier words, in
    isolation, still read like a quota rejection."""
    if sig.get("watchdog"):
        return health_model.STATE_STUCK_SUSPECTED, "turn_watchdog_fired", None
    if failure_class == CLASS_CONFIG_BLOCKED:
        return health_model.STATE_ERRORED_AMBIGUOUS, _setup_failure_reason(sig) or "config_blocked", None
    if (usage_limit_evidence is not None and failure_class == CLASS_INFRA
            and not usage_park.local_cause_present(sig)
            and usage_park.fact_from_stream(sig.get("usage_stream")) is not None):
        reason, detail = usage_limit_evidence
        return health_model.STATE_RATE_LIMITED_OR_OUTAGE, reason, detail
    if failure_class == CLASS_GATEWAY_HELD:
        # Transient, operator-resolvable gateway hold (#62): honestly an outage-like, retryable
        # wait - NOT an error and NOT idle. Distinct reason_code so status/doctor show a worker
        # blocked-on-a-held-gateway (that will self-heal on clear), not a frozen or dead one.
        # Checked before the sig["error"] branch below, which the hold also sets.
        return health_model.STATE_RATE_LIMITED_OR_OUTAGE, "gateway_held", None
    if sig.get("error"):
        return health_model.STATE_RATE_LIMITED_OR_OUTAGE, "spawn_exec_error", None
    rc = sig.get("rc")
    if isinstance(rc, int) and rc != 0 and not sig.get("terminal"):
        return health_model.STATE_CRASHED_OR_EXITED, "child_nonzero_exit", None
    if failure_class == CLASS_INFRA:
        reason, detail = _infra_reason(sig)
        return health_model.STATE_RATE_LIMITED_OR_OUTAGE, reason, detail
    if failure_class == CLASS_POISON:
        return health_model.STATE_ERRORED_POISON, "poison_eligible_failure", None
    if failure_class == CLASS_AMBIGUOUS:
        if sig.get("started") and not sig.get("completed"):
            return health_model.STATE_ERRORED_AMBIGUOUS, "partial_stream", None
        return health_model.STATE_ERRORED_AMBIGUOUS, "ambiguous_failure", None
    return health_model.STATE_UNKNOWN, "health_classifier_error", None
