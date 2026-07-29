"""Pure, fail-closed extraction of overall coverage from process stdout (#60 inc-3).

For built-in ``str`` or ``bytes`` sources, every ordinary data-dependent ``Exception`` raised
while decoding, scanning, or coercing coverage output is contained: the public parser returns a
finite ``float`` in [0, 100] or ``None``. Producer-controlled stdout is limited to 16 MiB at
this parse boundary. The subprocess capture is not bounded before parsing (#106), so callers
must not mistake this for a capture-time memory bound. Process-control ``BaseException``
signals (for example ``KeyboardInterrupt``, ``SystemExit``, and ``GeneratorExit``)
intentionally remain outside this contract rather than being misreported as malformed evidence.

Accepted text uses LF or CRLF line endings and contains one complete coverage.py ``TOTAL``
row (exactly two or four integer count columns) or one complete pytest-cov summary line.
The pytest-cov class includes its native fail-under sentence and the legacy/custom bare
``Total coverage:`` form. Percentages are ASCII integers or dot decimals. Bounded SGR color
sequences may decorate the summary; the final recognized summary across all forms wins.
Bare-carriage-return rewrites and every remaining terminal control fail closed. Direct byte
input must decode as UTF-8 with an optional BOM.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

__all__ = ["MAX_COVERAGE_ARTIFACT_BYTES", "parse_coverage_percent"]

MAX_COVERAGE_ARTIFACT_BYTES = 16 * 1024 * 1024
# Parse-time budget for the sole producer-controlled coverage evidence channel.
# SGR color parameters are normally only a few characters. The explicit cap
# keeps normalization bounded and leaves every other terminal control untrusted.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;:]{0,32}m")
_UNSUPPORTED_TERMINAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PERCENT_PATTERN = r"[0-9]+(?:\.[0-9]+)?"
# coverage.py emits exactly two count columns without branch coverage and four with it:
#   TOTAL                         1234     56    96%
#   TOTAL                         1234     56    200     12    94%
_TOTAL_LINE = re.compile(
    rf"^[ \t]*TOTAL[ \t]+[0-9]+[ \t]+[0-9]+"
    rf"(?:[ \t]+[0-9]+[ \t]+[0-9]+)?"
    rf"[ \t]+({_PERCENT_PATTERN})%[ \t]*$",
    re.MULTILINE,
)
# A legacy/custom bare pytest-cov summary retained as a complete-line form.
_TOTAL_COVERAGE = re.compile(
    rf"^[ \t]*Total coverage:[ \t]*({_PERCENT_PATTERN})%[ \t]*$",
    re.MULTILINE,
)
# pytest-cov's native --cov-fail-under terminal summaries. A failed run adds
# both "FAIL " and "not"; process outcome, not this text, determines success.
_PYTEST_COV_SUCCESS = re.compile(
    rf"^[ \t]*Required test coverage of[ \t]+"
    rf"{_PERCENT_PATTERN}%[ \t]+reached\.[ \t]+"
    rf"Total coverage:[ \t]*({_PERCENT_PATTERN})%[ \t]*$",
    re.MULTILINE,
)
_PYTEST_COV_FAILURE = re.compile(
    rf"^[ \t]*FAIL[ \t]+Required test coverage of[ \t]+"
    rf"{_PERCENT_PATTERN}%[ \t]+not[ \t]+reached\.[ \t]+"
    rf"Total coverage:[ \t]*({_PERCENT_PATTERN})%[ \t]*$",
    re.MULTILINE,
)


def _valid(pct: int | float | Decimal | str | None) -> float | None:
    """Return a finite percentage without ever rounding its decimal value upward.

    Gate evidence is JSON-compatible ``float`` data. If nearest-float conversion would
    serialize to a decimal greater than the producer's exact token, step down one
    representable float. This can conservatively understate coverage but cannot move it
    across a configured floor toward passing.
    """
    if pct is None or isinstance(pct, bool):
        return None
    try:
        exact = Decimal(str(pct)) if isinstance(pct, float) else Decimal(pct)
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        return None
    if not exact.is_finite() or exact < 0 or exact > 100:
        return None
    f = float(exact)
    if math.isnan(f) or math.isinf(f):
        return None
    if Decimal(str(f)) > exact:
        f = math.nextafter(f, -math.inf)
        if Decimal(str(f)) > exact:
            return None
    return f


def _evidence_within_limit(text: str) -> bool:
    if len(text) > MAX_COVERAGE_ARTIFACT_BYTES:
        return False
    try:
        return len(text.encode("utf-8")) <= MAX_COVERAGE_ARTIFACT_BYTES
    except UnicodeEncodeError:
        return False


def _source_text(source: str | bytes) -> str | None:
    if isinstance(source, bytes):
        if len(source) > MAX_COVERAGE_ARTIFACT_BYTES:
            return None
        return source.decode("utf-8-sig")
    if not isinstance(source, str):
        return None
    return source.removeprefix("\ufeff")


def _from_stdout(stdout: str) -> float | None:
    """Return the final structurally recognized summary by stream position."""
    if not _evidence_within_limit(stdout):
        return None

    normalized = _ANSI_SGR.sub("", stdout).replace("\r\n", "\n")
    if "\r" in normalized or _UNSUPPORTED_TERMINAL_CONTROL.search(normalized):
        return None

    last_match: tuple[int, str] | None = None
    for pattern in (
        _TOTAL_LINE,
        _TOTAL_COVERAGE,
        _PYTEST_COV_SUCCESS,
        _PYTEST_COV_FAILURE,
    ):
        for match in pattern.finditer(normalized):
            candidate = (match.end(), match.group(1))
            if last_match is None or candidate[0] > last_match[0]:
                last_match = candidate
    return _valid(last_match[1]) if last_match is not None else None


def parse_coverage_percent(stdout: str | bytes) -> float | None:
    """Return the overall line-coverage percentage (0.0-100.0), or ``None`` if it cannot be
    confidently determined.

    Stdout is the sole evidence channel. ``None`` means "coverage unknown" -> the DoD
    ``coverage`` dimension HOLDs. Root JSON and XML reports are deliberately not read. For
    built-in ``str`` and ``bytes`` sources, ordinary parser exceptions are contained at this
    public boundary; process-control ``BaseException`` signals intentionally propagate.
    Producer callers pass only stdout. Recognized forms must occupy a complete line and
    carry the documented numeric structure; incidental prose is not evidence. Stderr-only
    summaries, locale-comma decimals, and terminal progress rewrites are outside the
    accepted input class.
    """
    if not isinstance(stdout, (str, bytes)) or not stdout:
        return None
    try:
        text = _source_text(stdout)
        if not text:
            return None
        return _valid(_from_stdout(text))
    except Exception:  # noqa: BLE001 - enforce the public fail-closed parser boundary
        return None
