"""Pure, fail-closed extraction of overall coverage from process stdout (#60 inc-3).

For built-in ``str`` or ``bytes`` sources, every ordinary data-dependent ``Exception`` raised
while decoding, scanning, or coercing coverage output is contained: the public parser returns a
finite ``float`` in [0, 100] or ``None``. Producer-controlled stdout is limited to 16 MiB at
this parse boundary. The subprocess capture is not bounded before parsing (#106), so callers
must not mistake this for a capture-time memory bound. Process-control ``BaseException``
signals (for example ``KeyboardInterrupt``, ``SystemExit``, and ``GeneratorExit``)
intentionally remain outside this contract rather than being misreported as malformed evidence.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

__all__ = ["MAX_COVERAGE_ARTIFACT_BYTES", "parse_coverage_percent"]

MAX_COVERAGE_ARTIFACT_BYTES = 16 * 1024 * 1024
# Parse-time budget for the sole producer-controlled coverage evidence channel.
# coverage.py / pytest-cov terminal summary: a "TOTAL" row whose last token is a percent.
#   TOTAL                        1234    56    96%
_TOTAL_LINE = re.compile(r"^TOTAL\b.*?(?<!\S)([0-9]+(?:\.[0-9]+)?)%\s*$", re.MULTILINE)
# pytest-cov "Total coverage: 87.34%" (fractional) form.
_TOTAL_COVERAGE = re.compile(
    r"\bTotal coverage:\s*([0-9]+(?:\.[0-9]+)?)%(?=\s|$)",
    re.IGNORECASE,
)


def _valid(pct: int | float | Decimal | str | None) -> float | None:
    """Accept only a real number in [0, 100]; anything else is fail-closed ``None``."""
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
    """coverage.py ``TOTAL ... 96%`` or pytest-cov ``Total coverage: 87.34%``. Last match wins."""
    if not _evidence_within_limit(stdout):
        return None
    frac = None
    matches = _TOTAL_COVERAGE.findall(stdout)
    if matches:
        frac = matches[-1]
    else:
        totals = _TOTAL_LINE.findall(stdout)
        if totals:
            frac = totals[-1]
    if frac is None:
        return None
    return _valid(frac)


def parse_coverage_percent(stdout: str | bytes) -> float | None:
    """Return the overall line-coverage percentage (0.0-100.0), or ``None`` if it cannot be
    confidently determined.

    Stdout is the sole evidence channel. ``None`` means "coverage unknown" -> the DoD
    ``coverage`` dimension HOLDs. Root JSON and XML reports are deliberately not read. For
    built-in ``str`` and ``bytes`` sources, ordinary parser exceptions are contained at this
    public boundary; process-control ``BaseException`` signals intentionally propagate.
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
