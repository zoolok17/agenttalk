"""Stopping retries at a provider usage limit: the pure rules.

When a Claude seat runs out of its allowance, today's wrapper starts the model again
and again for hours and then dead-letters the message. With this module's rules the
wrapper PARKS the message instead: no retries, the message kept, the seat alive. It
tries once each time the wrapper is started again, and once at the time Claude
states the allowance comes back (plus a safety margin).

Everything here is a plain function over plain values, so the loop and the store
only wire it in:

* the switch (``AGENTTALK_STOP_RETRIES_AT_LIMIT``, on unless set to ``0``);
* the proof read from ONE invocation's own stream-json (a rejected usage event with a
  known window, then a final result that is an error) - never from message text;
* the sanity rules for the stated reset time and the wake time;
* the changes to a message's attempt record (counters that never count toward
  disposal while parked, the probe marker, the wake rules).
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone

SWITCH_ENV = "AGENTTALK_STOP_RETRIES_AT_LIMIT"

# The failure word a parked head carries in its attempt record, and the fact word a
# drive attaches to a failure it can prove is a usage limit. Never "config_blocked".
CLASS_USAGE_LIMIT = "usage_limit"
FACT_USAGE_LIMIT = "usage_limit"
# Health reason (the existing rate_limited_or_outage state carries it).
REASON_PARKED = "usage_limit_parked"
# The marker's state word.
MARKER_STATE = "usage_limit_parked"

# The only window names seen in real captured cases. Anything else is not proof.
KNOWN_WINDOWS = ("five_hour", "seven_day")

# The wake comes this long after the reset Claude states.
WAKE_MARGIN_SECONDS = 30
# A stated reset must lie in the future and at most this far ahead (the weekly window
# is 7 days); otherwise it is treated as unknown and there is no timed wake.
MAX_RESET_AHEAD_SECONDS = 8 * 86400

# The published marker: refreshed at most this often while parked, and read as stale
# (but still shown) when it is older than the stale limit.
MARKER_REFRESH_SECONDS = 60.0
MARKER_STALE_SECONDS = 300.0

# The operator notice, per park transition: retried at most this many times, this far
# apart, while it cannot be routed.
NOTICE_RETRY_SECONDS = 900.0
NOTICE_MAX_TRIES = 4

# doctor warns that a seat has been parked for a long time after this many hours (the
# environment variable below overrides it, in hours).
PARK_WARN_AFTER_HOURS = 24.0
PARK_WARN_ENV = "AGENTTALK_USAGE_PARK_WARN_AFTER_HOURS"

PARKED = "parked"
PROBING = "probing"
PARK_STATES = (PARKED, PROBING)

_OFF_WORDS = frozenset({"0", "false", "off", "no"})


def enabled(environ=None) -> bool:
    """True unless the switch is set to ``0`` (also ``false``, ``off``, ``no``)."""
    env = os.environ if environ is None else environ
    return str(env.get(SWITCH_ENV, "1")).strip().lower() not in _OFF_WORDS


# The largest integer treated as a number at all: a JSON integer may have hundreds of digits,
# and converting it to a float raises, so it is refused BEFORE any conversion. No time in
# seconds, and no utilization, needs more than this.
_MAX_INT = 2 ** 63


def _number(value: object) -> float | None:
    """A finite number as a float, or None. Never raises: an integer too large to be a
    real reading is refused before it is converted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value) if abs(value) <= _MAX_INT else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def whole_seconds(value: object) -> int | None:
    """A time as whole, positive seconds since 1970; text, booleans, fractions and huge
    numbers are no. Never raises."""
    number = _number(value)
    if number is None or number <= 0 or number != int(number):
        return None
    return int(value) if isinstance(value, int) else int(number)


def marker_time(now_epoch: object = None) -> int:
    """THE one precision of every time in the published marker: whole seconds, floored
    (the wrapper's clock has fractions; the reader accepts only whole seconds, so the writer
    floors and the two can never disagree). A missing or unusable clock reading uses the
    current time. Never raises."""
    number = _number(now_epoch)
    if number is None or number <= 0:
        number = time.time()
    return int(math.floor(number))


def _exhausted_reset(window: object) -> tuple[bool, int | None]:
    """(is_exhausted, reset) for one entry of ``unifiedWindows``."""
    if not isinstance(window, dict):
        return False, None
    utilization = _number(window.get("utilization"))
    if utilization is None or utilization < 1.0:
        return False, None
    return True, whole_seconds(window.get("resetsAt"))


def _rejected_event(info: dict) -> dict | None:
    """The proof fields of one rejected usage event, or None when it is not one."""
    if info.get("status") != "rejected":
        return None
    window = info.get("rateLimitType")
    if window not in KNOWN_WINDOWS:
        return None
    reset = whole_seconds(info.get("resetsAt"))
    resets = [] if reset is None else [reset]
    known = reset is not None          # an exhausted window with no usable reset: no reset at all
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name in KNOWN_WINDOWS:
            exhausted, other = _exhausted_reset(unified.get(name))
            if exhausted:
                if other is None:
                    known = False
                else:
                    resets.append(other)
    return {"window": window, "reset_epoch": max(resets) if (known and resets) else None}


def note_stream_event(state: dict, raw: object) -> None:
    """Fold one parsed stream-json object into ``state`` (one invocation's own stream).

    Keeps only structure: the last rejected usage event (its known window and the
    latest exhausted-window reset) and whether the LAST terminal result after it is an
    error (``is_error`` exactly ``true``), a success (exactly ``false``) or neither.
    Never reads ``result``, message text or ``subtype``."""
    if not isinstance(raw, dict):
        return
    kind = raw.get("type")
    if kind == "rate_limit_event":
        info = raw.get("rate_limit_info")
        rejected = _rejected_event(info) if isinstance(info, dict) else None
        if rejected is not None:
            state["rejected"] = rejected
            state["result_is_error"] = None     # a result must come AFTER the event
    elif kind == "result" and state.get("rejected") is not None:
        flag = raw.get("is_error")
        state["result_is_error"] = flag if isinstance(flag, bool) else "other"


def fact_from_stream(state: object) -> dict | None:
    """``{"window", "reset_epoch"}`` when the stream proves the provider ended the turn
    on a usage limit, else None. The proof: a rejected event with a known window, then a
    later terminal result whose ``is_error`` is exactly ``true``."""
    if not isinstance(state, dict):
        return None
    rejected = state.get("rejected")
    if rejected is None or state.get("result_is_error") is not True:
        return None
    return {"window": rejected["window"], "reset_epoch": rejected["reset_epoch"]}


def usable_reset(reset: object, now_epoch: float) -> int | None:
    """The stated reset if it lies in the future and at most 8 days ahead, else None."""
    seconds = whole_seconds(reset)
    if seconds is None or seconds <= now_epoch or seconds > now_epoch + MAX_RESET_AHEAD_SECONDS:
        return None
    return seconds


# ------------------------------------------------------------------ the attempt record


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def iso_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def disposal_attempts(rec: dict | None) -> int:
    """Attempts that count toward any disposal decision: the lifetime launch count minus
    the attempts made under a park (the one that found the limit and every probe)."""
    rec = rec or {}
    return max(0, _int(rec.get("attempts_started")) - _int(rec.get("excluded_attempts")))


def parked_seconds(rec: dict | None) -> float:
    """Seconds spent parked in parks that have closed."""
    value = _number((rec or {}).get("parked_seconds_total"))
    return max(0.0, value) if value is not None else 0.0


def is_parked(rec: dict | None) -> bool:
    return (rec or {}).get("park_state") == PARKED


def wake_due(rec: dict, now_epoch: float) -> bool:
    """True when a wake time is set, has come, and no probe consumed it yet."""
    wake = whole_seconds(rec.get("wake_epoch"))
    return wake is not None and now_epoch >= wake and rec.get("probed_wake_epoch") != wake


def unused_wake(rec: dict) -> int | None:
    """The wake time a reader may show: set and not yet consumed by a probe."""
    wake = whole_seconds(rec.get("wake_epoch"))
    return wake if wake is not None and rec.get("probed_wake_epoch") != wake else None


def apply_probe_start(rec: dict, *, generation: str, consumed_wake: int | None) -> None:
    """The write-ahead of a probe, in the same write as the attempt start: the attempt is
    excluded from disposal, the marker says a probe is in flight, and the wake (if it was
    due) is consumed - one attempt consumes both triggers."""
    rec["park_state"] = PROBING
    rec["probe_marker"] = True
    rec["parked_generation"] = generation
    rec["excluded_attempts"] = _int(rec.get("excluded_attempts")) + 1
    if consumed_wake is not None:
        rec["probed_wake_epoch"] = consumed_wake


def apply_limit_result(rec: dict, *, at: str, generation: str, window: str,
                       reset_epoch: int | None) -> None:
    """A usage-limit result: park (or stay parked after a probe). No failure counter moves.

    The wake rule: only a reset STRICTLY LATER than the latest one seen sets (or
    replaces) the wake; the same or an earlier reset leaves any existing wake as it is
    (an unused one stays; a consumed one is never re-armed)."""
    was_parked = rec.get("park_state") in PARK_STATES
    if not was_parked:
        rec["parked_at"] = at
        rec["park_count"] = _int(rec.get("park_count")) + 1
        rec["excluded_attempts"] = _int(rec.get("excluded_attempts")) + 1
    rec["park_state"] = PARKED
    rec["probe_marker"] = False
    rec["parked_generation"] = generation
    rec["limit_failures"] = _int(rec.get("limit_failures")) + 1
    rec["limit_window"] = window
    last = whole_seconds(rec.get("last_reset_epoch"))
    if reset_epoch is not None and (last is None or reset_epoch > last):
        rec["reset_epoch"] = reset_epoch
        rec["last_reset_epoch"] = reset_epoch
        rec["wake_epoch"] = reset_epoch + WAKE_MARGIN_SECONDS
    count = _int(rec.get("park_count"))
    rec["notice_key"] = (f"probe:{count}:{_int(rec.get('limit_failures'))}" if was_parked
                         else f"park:{count}")
    rec["notice_routed"] = False
    rec["notice_tries"] = 0
    rec["notice_next_at"] = None


_PARK_KEYS = ("park_state", "probe_marker", "parked_generation", "reset_epoch", "wake_epoch",
              "limit_window", "notice_key", "notice_routed", "notice_tries", "notice_next_at")


def apply_park_close(rec: dict, *, at_epoch: float | None) -> None:
    """The park is over (a probe met another class, or the switch is off and the head is
    driven): the time spent parked is added to the total, the park fields go. The
    lifetime counters, the excluded count and the latest reset seen stay."""
    parked_at = iso_epoch(rec.get("parked_at"))
    if parked_at is not None and at_epoch is not None and at_epoch > parked_at:
        rec["parked_seconds_total"] = parked_seconds(rec) + (at_epoch - parked_at)
    rec.pop("parked_at", None)
    for key in _PARK_KEYS:
        rec.pop(key, None)


def apply_crash_reconcile(rec: dict) -> None:
    """A crash while a probe was in flight: park again. The probe was already counted as
    excluded, and its wake was consumed, so a restart gives one start probe and no more."""
    rec["in_progress"] = False
    rec["probe_marker"] = False
    rec["park_state"] = PARKED


# ------------------------------------------------------------------ what readers show

# Health states that are CURRENT evidence of a running or stuck turn: a fresh park marker
# never overrides them (a probe is running, or the wrapper is wedged).
_CURRENT_WORK_STATES = ("working_turn", "working_silent", "stuck_suspected")

VIEW_PARKED = "parked"
VIEW_STALE = "stale"


def format_epoch(epoch: object) -> str | None:
    """A time for people, in UTC ("2026-09-09 10:00 UTC"), or None for a bad value."""
    seconds = whole_seconds(epoch)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return None


def park_view(marker: dict | None, health: dict | None = None) -> dict | None:
    """What every reader of seat health shows for a parked seat, from the published marker.

    None when there is no marker, or when the health file is CURRENT evidence of a turn
    running or stuck (the marker never overrides that). A marker the wrapper stopped
    refreshing is NOT dropped: it reads as ``stale`` ("wrapper not responding"), so a dead
    parked seat never looks healthy. Closed words, numbers and times only."""
    if not isinstance(marker, dict):
        return None
    fresh = bool(marker.get("fresh"))
    if (fresh and isinstance(health, dict) and not health.get("stale")
            and health.get("state") in _CURRENT_WORK_STATES):
        return None
    return {
        "present": True,
        "state": VIEW_PARKED if fresh else VIEW_STALE,
        "fresh": fresh,
        "window": marker.get("window"),
        "reset_epoch": marker.get("reset_epoch"),
        "wake_epoch": marker.get("wake_epoch"),
        "message_id": marker.get("message_id"),
        "parked_at": marker.get("parked_at"),
        "age_seconds": marker.get("age_seconds"),
    }


def park_text(view: dict | None) -> str | None:
    """The one line every reader uses for a parked seat. Never "config blocked"."""
    if not isinstance(view, dict) or not view.get("present"):
        return None
    if view.get("state") == VIEW_STALE:
        return "parked on a usage limit, wrapper not responding"
    when = format_epoch(view.get("reset_epoch")) if view.get("wake_epoch") else None
    return f"parked on a usage limit until {when}" if when else "parked on a usage limit until restarted"
