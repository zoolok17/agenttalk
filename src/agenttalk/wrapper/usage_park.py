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
import re
import time
from datetime import datetime, timezone

from agenttalk import health as _health

SWITCH_ENV = "AGENTTALK_STOP_RETRIES_AT_LIMIT"

# The failure word a parked head carries in its attempt record, and the fact word a
# drive attaches to a failure it can prove is a usage limit. Never "config_blocked".
CLASS_USAGE_LIMIT = "usage_limit"
FACT_USAGE_LIMIT = "usage_limit"
# Health reason (the existing rate_limited_or_outage state carries it).
REASON_PARKED = "usage_limit_parked"
# The marker's state word.
MARKER_STATE = "usage_limit_parked"

# The published marker's contract version, and the closed set of providers it may name. A
# provider is only ever taken from the proof the wrapper holds (a Claude usage event proves
# "claude"), never from an agent name.
MARKER_SCHEMA_VERSION = 1
PROVIDER_CLAUDE = "claude"
PROVIDERS = (PROVIDER_CLAUDE,)

# The documented version-1 contract (README "Reading the park marker from another program"):
# exactly these eleven keys, every time, nullable ones included. The reader requires every KEY
# to be present (not merely non-null when read with .get()) - a file missing one is a damaged
# write (the documented contract says a reader must retry it), never silently "the field is
# null". #311 connector 4174800511.
MARKER_KEYS = frozenset({
    "schema_version", "agent", "state", "provider", "window", "reset_epoch", "wake_epoch",
    "message_id", "parked_at", "wrapper_generation", "updated_at_epoch",
})

# The only window names seen in real captured cases. Anything else is not proof.
KNOWN_WINDOWS = ("five_hour", "seven_day")

# The wake comes this long after the reset Claude states.
WAKE_MARGIN_SECONDS = 30
# A stated reset must lie in the future and at most this far ahead (the weekly window
# is 7 days); otherwise it is treated as unknown and there is no timed wake.
MAX_RESET_AHEAD_SECONDS = 8 * 86400

# The published marker: refreshed at most this often while parked, and read as stale
# (but still shown) when it is older than the stale limit.
#
# Tolerance, stated: every time in the marker is FLOORED to whole seconds (``marker_time``), by
# the writer and for the reader's clock alike, so an age is exact only to within one second. A
# marker written at T+0.1 and read at T+300.9 is 300.8 s old but still counts as fresh: a
# "stale" label can come up to one second LATE, and a fresh marker never turns stale EARLY.
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


# The largest magnitude treated as a number at all, for integers AND floats: a JSON integer may
# have hundreds of digits (converting it to a float raises, so it is refused BEFORE any
# conversion) and a JSON float may be finite but absurd (1e308 would read as a time far beyond
# any clock). No time in seconds, and no utilization, needs more than this.
_MAX_MAGNITUDE = 2 ** 63


def _number(value: object) -> float | None:
    """A finite number within the bound as a float, or None. Never raises."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value) if abs(value) <= _MAX_MAGNITUDE else None
    if isinstance(value, float):
        return value if math.isfinite(value) and abs(value) <= _MAX_MAGNITUDE else None
    return None


def whole_seconds(value: object) -> int | None:
    """A time as whole, positive seconds since 1970; text, booleans, fractions and huge
    numbers are no. Never raises."""
    number = _number(value)
    if number is None or number <= 0 or number != int(number):
        return None
    return int(value) if isinstance(value, int) else int(number)


# The latest epoch second Python's own datetime can show (datetime(9999, 12, 31, 23, 59, 59,
# tzinfo=utc).timestamp()). The tighter bound in practice: a reset far enough in the future to
# overflow JS's Date (whose own range is far wider) never gets there first, but Windows'
# C runtime already raises OSError well before this ceiling for some early dates, which is why
# every CALLER that turns a marker epoch into a displayed date must still guard its own
# conversion (see ``epoch_iso`` and the console formatters) - this bound only catches the
# numbers that are never displayable by ANY reader, not every number a given platform refuses.
MAX_DISPLAYABLE_EPOCH = 253_402_300_799


def displayable_epoch(value: object) -> int | None:
    """``whole_seconds(value)``, additionally refusing anything past the furthest date any
    known reader (Python's datetime, JS's Date) can show. Never raises."""
    seconds = whole_seconds(value)
    return seconds if seconds is not None and seconds <= MAX_DISPLAYABLE_EPOCH else None


def displayable_iso(value: object) -> str | None:
    """``value`` unchanged if it is text that names a displayable time, else None. Never
    raises - a string that merely PARSES (``"0001-01-01"``) but whose epoch a real reader
    cannot show (``.timestamp()`` raises OSError for some early dates on Windows) is also
    refused, not just one that fails to parse at all."""
    if not isinstance(value, str) or not value:
        return None
    epoch = iso_epoch(value)
    if epoch is None or epoch < 0 or epoch > MAX_DISPLAYABLE_EPOCH:
        return None
    return value


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


def _combine_exhausted_resets(pairs: object) -> int | None:
    """THE one combining rule, shared by every caller that must answer "what is the
    seat's recovery time, given several allowance windows": ``(is_exhausted, reset)``
    pairs in, the latest reset out - UNLESS any exhausted window has no usable reset at
    all, in which case the answer is None, even when another exhausted window DOES have
    one. A partial, possibly-too-early answer is worse than admitting the true recovery
    time cannot be established (#305 fix round 1, connector 4178374143) - a seat still
    blocked by an exhausted window with an unknown reset is not "recovering on schedule"
    just because a DIFFERENT exhausted window happens to have a known one."""
    best: int | None = None
    for is_exhausted, reset in pairs:
        if not is_exhausted:
            continue
        if reset is None:
            return None
        if best is None or reset > best:
            best = reset
    return best


def _rejected_event(info: dict) -> dict | None:
    """The proof fields of one rejected usage event, or None when it is not one."""
    if info.get("status") != "rejected":
        return None
    window = info.get("rateLimitType")
    if window not in KNOWN_WINDOWS:
        return None
    # The rejected window itself always counts as exhausted (it is literally the one
    # that was refused), beside every OTHER window ``unifiedWindows`` names as exhausted.
    pairs = [(True, whole_seconds(info.get("resetsAt")))]
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        pairs.extend(_exhausted_reset(unified.get(name)) for name in KNOWN_WINDOWS)
    return {"window": window, "reset_epoch": _combine_exhausted_resets(pairs)}


# Structured, closed evidence of transient provider trouble that is NOT a usage limit
# (#305): a 429/rate_limit_error is the provider briefly throttling; a 529/overloaded_error
# is the provider's own capacity problem. Neither implies the account's allowance is low.
# Read from the wrapper loop's OWN already-extracted structured-error facts
# (run.py's sig["structured_errors"], built by its existing Claude error extractor) -
# health.classify_failure reuses this, never a second parser for the same fields.
HTTP_STATUS_THROTTLED = 429
HTTP_STATUS_OVERLOADED = 529
SUBTYPE_THROTTLED = "rate_limit_error"
SUBTYPE_OVERLOADED = "overloaded_error"


def usage_limit_rejected_window(rate_limit_info: object) -> str | None:
    """The known window name (``five_hour``/``seven_day``) if ``rate_limit_info`` is the
    SAME structured proof the usage-limit park decision already trusts (a rejected
    ``rate_limit_event`` naming a known window), else None. Reuses ``_rejected_event``'s
    exact proof rule - the health label (#305) must never build a second parser for the
    same fact ``note_stream_event``/``fact_from_stream`` already establish for parking."""
    if not isinstance(rate_limit_info, dict):
        return None
    fact = _rejected_event(rate_limit_info)
    return fact["window"] if fact is not None else None


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


def local_cause_present(sig: dict) -> bool:
    """True when ``sig`` carries a LOCAL cause (a watchdog kill, a configuration refusal,
    a bus-write fault, a held gateway) - one that keeps its own class and must veto ANY
    provider-side usage-limit refinement, whether that refinement is the park decision's
    own fact (``run._usage_limit_fact``) or health's retained-evidence override
    (``health.classify_failure``, #305 fix round 1, connector 4178374147). THE one
    condition, shared, so neither caller can drift from what the other already excludes -
    a rejected quota event followed by anything other than a genuine provider failure
    proves nothing about why THIS turn actually ended."""
    return bool(
        sig.get("watchdog") or sig.get("config_blocked") or sig.get("bus_failure") is not None
        or sig.get("setup_failure") is not None or sig.get("gateway_transient_hold")
    )


def usable_reset(reset: object, now_epoch: float) -> int | None:
    """The stated reset if it lies in the future and at most 8 days ahead, else None."""
    seconds = whole_seconds(reset)
    if seconds is None or seconds <= now_epoch or seconds > now_epoch + MAX_RESET_AHEAD_SECONDS:
        return None
    return seconds


def latest_exhausted_reset(windows: object, *, now_epoch: float) -> int | None:
    """#305 F10 (recast): the seat-level recovery time, from CURRENT evidence - the latest
    USABLE reset among every window that is exhausted RIGHT NOW, or None when that cannot
    be established (no window is exhausted, OR an exhausted window has no usable reset -
    the SAME unknown-reset veto ``_rejected_event`` applies, via the shared
    :func:`_combine_exhausted_resets`; fix round 1, connector 4178374143: skipping an
    exhausted-but-unknown window used to let a DIFFERENT exhausted window's own, possibly
    much-sooner reset stand in as the seat's recovery time, which is not established at
    all when the seat may still be blocked by the unknown one).

    This is the live-reading counterpart of ``_rejected_event``'s own rule (there applied
    once, to the provider's frozen ``unifiedWindows`` fact at the moment of rejection;
    here applied to the agent's live capacity reading, since a seat-level recovery time
    must reflect what is true NOW - a window exhausted at rejection time may since have
    reset, and a window that was NOT exhausted then may be now). The two normalize two
    different input shapes from two different sources (a fractional provider snapshot vs.
    a live capacity percentage) into the SAME ``(is_exhausted, reset)`` shape the shared
    combiner reads - never two independent rules; a caller must never pick a single named
    window's own reset instead.

    ``windows``: an iterable of ``(used_pct, resets_at)`` pairs. A window counts as exhausted
    at ``used_pct >= 100``; its reset counts only through :func:`usable_reset` (future, not
    absurdly distant) - exactly the same bound the park marker's own reset already uses."""
    pairs = []
    for used_pct, resets_at in windows:
        exhausted = isinstance(used_pct, (int, float)) and used_pct >= 100
        reset = usable_reset(resets_at, now_epoch) if exhausted else None
        pairs.append((exhausted, reset))
    return _combine_exhausted_resets(pairs)


# ------------------------------------------------------------------ the attempt record


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


#: Python 3.10's datetime.fromisoformat refuses a fractional-seconds part that is not
#: exactly 3 or 6 digits; 3.11+ already accepts any length there (silently keeping only
#: microsecond precision). Pad or truncate the SECONDS field's own fraction to 6 digits
#: before parsing, so 3.10 reads it the same way every newer version already does (same
#: normalisation as capacity._normalize_ts and supervisor_lifecycle.start_tokens_match).
#: Anchored to ``hh:mm:ss.`` on one side and the offset (``Z``/``+``/``-``) or the string's
#: end on the other, so this can only ever match the SECONDS field's own fraction - never a
#: date/time separator that happens to be a dot (``2026-10-03.12:00:00Z`` is valid ISO 8601;
#: fromisoformat accepts any single separator character) and never turn a malformed value
#: into a different, valid one (round 4 regression, tk-5a6466468f8e: an earlier unanchored
#: version matched ".12" in ``2026-10-03.12:00:00Z`` as if it were a two-digit fraction).
_FRACTION_RE = re.compile(r"(?<=\d\d:\d\d:\d\d)\.(\d+)(?=Z|[+-]|\Z)")


def iso_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # #311 connector 4175000405: a naive timestamp (no explicit timezone) has no agreed
        # meaning - .timestamp() on one is interpreted in the PLATFORM's local time, silently
        # wrong wherever the wrapper's own clock is not UTC. Every marker/attempt time this
        # wrapper writes already carries an explicit offset (epoch_iso always appends "Z");
        # a value without one is refused, not guessed at.
        return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError):
        # a date that PARSES but whose .timestamp() the platform's C runtime cannot represent
        # (some early dates on Windows), or one outside C's time_t range on some platforms.
        return None


def epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def disposal_attempts(rec: dict | None) -> int:
    """Attempts that count toward any disposal decision: the lifetime launch count minus
    the attempts made under a park (the one that found the limit and every probe).

    Both persisted counters are validated non-negative, and the exclusion is clamped to the
    launch count, BEFORE the subtraction: a negative (damaged or hand-edited) exclusion must
    never increase the effective count past what was actually launched (#311 connector
    4175000411 - a switch-OFF read of attempts_started=1, excluded_attempts=-20 must read as
    1 eligible attempt, never 21)."""
    rec = rec or {}
    started = max(0, _int(rec.get("attempts_started")))
    excluded = min(max(0, _int(rec.get("excluded_attempts"))), started)
    return started - excluded


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
                       reset_epoch: int | None, provider: str | None = None) -> None:
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
    if provider in PROVIDERS:
        rec["limit_provider"] = provider
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
              "limit_window", "limit_provider", "notice_key", "notice_routed", "notice_tries", "notice_next_at")


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
    """A time for people, in UTC ("2026-09-09 10:00 UTC"), or None for a bad value.

    Display only: any finite positive time is accepted and rounded down to the second (a
    park time carries fractions). The strict whole-second rule stays for the provider's
    reset proof (``whole_seconds``)."""
    number = _number(epoch)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(number), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return None


def recovery_text(agent: str, message_id: str) -> str:
    """THE one text that tells a person how to get a parked seat moving again. Attention,
    doctor and the notice all use it, and the README says the same."""
    return (
        f"To start it again now: agenttalk request-restart --for {agent} (it needs a running "
        "supervisor; without one, stop the wrapper and start it again). A protected seat (the "
        "operator-facing liaison or a lead) also needs --force-protected and, because a parked "
        "seat is alive, --acknowledge-live-protected-kill. To skip the parked message instead: "
        f"agenttalk ack --for {agent} --id {message_id} (it skips the message and leaves no "
        "dead-letter record; it is refused for a managed lead-loop agent).")


# Supervisor verdicts that confirm the seat healthy; any other verdict is independent adverse
# evidence (for example STUCK_OR_DEAD) and wins over a park.
_HEALTHY_VERDICTS = ("HEALTHY_IDLE", "HEALTHY_WORKING")

NOT_CONSULTED_NOTE = "supervisor not consulted"


def verdict_is_adverse(verdict_state: object) -> bool:
    """True for a supervisor verdict that says the seat is not healthy."""
    return isinstance(verdict_state, str) and bool(verdict_state) and verdict_state not in _HEALTHY_VERDICTS


def apply_verdict(view: dict | None, verdict_state: object) -> dict | None:
    """The one rule for a surface that already holds a park view and later learns the verdict
    (the final supervisor assessment): an adverse verdict removes the park."""
    return None if verdict_is_adverse(verdict_state) else view


def mark_not_consulted(view: dict | None) -> dict | None:
    """Label a view built where no supervisor verdict is available (attention queue, doctor):
    a limited observation, shown as such."""
    return None if view is None else {**view, "supervisor_consulted": False}


def _stale_work_history(health: object) -> bool:
    """Stale health whose own state, or last known state, was working or stuck."""
    return (isinstance(health, dict) and bool(health.get("stale"))
            and (health.get("state") in _CURRENT_WORK_STATES
                 or health.get("last_known_state") in _CURRENT_WORK_STATES))


def park_view(marker: dict | None, health: dict | None = None, *, verdict_state: object = None,
              heartbeat_age: object = None) -> dict | None:
    """What every reader of seat health shows for a parked seat, from the published marker.

    THE one precedence, decided here and shipped in the payload (no reader decides its own):
      1. an independent adverse supervisor verdict (anything but healthy) wins: no park;
      2. CURRENT (not stale) working or stuck health wins over any park, fresh or stale: no park;
      3. a fresh marker beats historical (stale) working health: ``parked``;
      4. a stale marker, or a fresh one whose seat has not stamped its heartbeat for as long,
         is ranked BELOW stale working history (no park: the old work is shown); with nothing
         stronger it shows as ``stale`` ("wrapper not responding"), never as healthy;
      5. no marker: nothing (today's display).
    Closed words, numbers and times only.

    #311 recast fix round 1, finding 1: freshness ALWAYS needs a present, finite heartbeat
    age within the allowed negative skew and the stale bound - never only "not too old". A
    MISSING heartbeat (``beat is None``) or one claiming to be from the FUTURE beyond
    ordinary clock skew used to pass this check silently (the old test was only
    ``beat > MARKER_STALE_SECONDS``), so a marker claiming ``fresh`` could still read as
    parked with no real liveness evidence behind it at all. This one bound is now the
    SAME for both the matching-marker branch (combined with the marker's own ``fresh``)
    and the no-marker fallback (the caller passes ``fresh=True`` there, so this bound is
    then the ENTIRE answer) - one gate, not two different ones per branch."""
    if not isinstance(marker, dict):
        return None
    if verdict_is_adverse(verdict_state):
        return None
    if isinstance(health, dict) and not health.get("stale") and health.get("state") in _CURRENT_WORK_STATES:
        return None
    beat = _number(heartbeat_age)
    heartbeat_ok = beat is not None and -_health.DEFAULT_HEARTBEAT_SKEW_SECONDS <= beat <= MARKER_STALE_SECONDS
    fresh = bool(marker.get("fresh")) and heartbeat_ok
    if not fresh and _stale_work_history(health):
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
        text = "parked on a usage limit, wrapper not responding"
    else:
        when = format_epoch(view.get("reset_epoch")) if view.get("wake_epoch") else None
        text = f"parked on a usage limit until {when}" if when else "parked on a usage limit until restarted"
    if view.get("supervisor_consulted") is False:
        text += f" ({NOT_CONSULTED_NOTE})"
    return text
