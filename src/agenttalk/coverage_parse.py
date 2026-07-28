"""Pure, fail-closed extraction of an overall coverage percentage (#60 inc-3).

For built-in ``str`` or ``bytes`` sources, every ordinary data-dependent ``Exception`` raised
while decoding, parsing, or coercing coverage output is contained: the public parser returns a
finite ``float`` in [0, 100] or ``None``. JSON artifacts and process stdout are
producer-controlled evidence and therefore size-bounded. Process-control ``BaseException``
signals (for example ``KeyboardInterrupt``, ``SystemExit``, and ``GeneratorExit``)
intentionally remain outside this contract rather than being misreported as malformed evidence.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation

__all__ = ["MAX_COVERAGE_ARTIFACT_BYTES", "parse_coverage_percent"]

MAX_COVERAGE_ARTIFACT_BYTES = 16 * 1024 * 1024
# Shared budget for every producer-controlled coverage evidence channel.
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


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _from_json(json_text: str) -> float | None:
    """coverage.py JSON: ``["totals"]["percent_covered"]`` (already 0-100)."""
    if not _evidence_within_limit(json_text):
        return None
    try:
        data = json.loads(
            json_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
        )
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    totals = data.get("totals")
    if not isinstance(totals, dict) or "percent_covered" not in totals:
        return None
    value = totals.get("percent_covered")
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return _valid(value)


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


def parse_coverage_percent(
    stdout: str | bytes,
    json_text: str | bytes | None = None,
) -> float | None:
    """Return the overall line-coverage percentage (0.0-100.0), or ``None`` if it cannot be
    confidently determined.

    Sources are consulted in priority order; the first that yields a valid number wins (we do
    NOT average disagreeing sources): coverage.json (``totals.percent_covered``), then the
    terminal summary in ``stdout``. ``None`` means "coverage unknown" -> the DoD ``coverage``
    dimension HOLDs. XML is deliberately not accepted as evidence. For built-in ``str`` and
    ``bytes`` sources, ordinary parser exceptions are contained at this public boundary;
    process-control ``BaseException`` signals intentionally propagate.
    """
    for parser, source in (
        (_from_json, json_text),
        (_from_stdout, stdout),
    ):
        if not isinstance(source, (str, bytes)) or not source:
            continue
        try:
            text = _source_text(source)
            if not text:
                continue
            pct = _valid(parser(text))
        except Exception:  # noqa: BLE001 - enforce the public fail-closed parser boundary
            pct = None
        if pct is not None:
            return pct
    return None
