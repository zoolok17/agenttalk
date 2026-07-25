"""Pure extraction of an overall coverage percentage from coverage-tool output (#60 inc-3).

The DoD ``coverage`` dimension needs a NUMBER, but the ``coverage`` assurance tool today only
yields pass/fail. This module turns tool output into a single ``float`` percentage (0-100) or
``None``. Coverage artifacts are producer-controlled evidence, so XML and JSON are treated as
untrusted input. The module is PURE (no I/O) and FAIL-CLOSED: any ambiguity or parse failure
returns ``None``, which the caller treats as "coverage unknown" so the gate HOLDs. It never raises.
"""

from __future__ import annotations

import json
import math
import re

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

__all__ = ["MAX_COVERAGE_ARTIFACT_BYTES", "parse_coverage_percent"]

MAX_COVERAGE_ARTIFACT_BYTES = 16 * 1024 * 1024
# coverage.py / pytest-cov terminal summary: a "TOTAL" row whose last token is a percent.
#   TOTAL                        1234    56    96%
_TOTAL_LINE = re.compile(r"^TOTAL\b.*?(?<!\S)([0-9]+(?:\.[0-9]+)?)%\s*$", re.MULTILINE)
# pytest-cov "Total coverage: 87.34%" (fractional) form.
_TOTAL_COVERAGE = re.compile(
    r"\bTotal coverage:\s*([0-9]+(?:\.[0-9]+)?)%(?=\s|$)",
    re.IGNORECASE,
)


def _valid(pct: float | None) -> float | None:
    """Accept only a real number in [0, 100]; anything else is fail-closed ``None``."""
    if pct is None:
        return None
    try:
        f = float(pct)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f < 0.0 or f > 100.0:
        return None
    return f


def _artifact_within_limit(text: str) -> bool:
    if len(text) > MAX_COVERAGE_ARTIFACT_BYTES:
        return False
    try:
        return len(text.encode("utf-8")) <= MAX_COVERAGE_ARTIFACT_BYTES
    except UnicodeEncodeError:
        return False


def _from_xml(xml_text: str) -> float | None:
    """Cobertura ``<coverage line-rate="0.8734" ...>`` (a 0-1 fraction) -> 87.34."""
    if not _artifact_within_limit(xml_text):
        return None
    try:
        root = ET.fromstring(
            xml_text,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, ET.ParseError, ValueError):
        return None
    rate = root.get("line-rate")
    if rate is None:
        return None
    try:
        frac = float(rate)
    except (TypeError, ValueError):
        return None
    # line-rate is a 0-1 fraction; reject an out-of-range value rather than guessing.
    if math.isnan(frac) or math.isinf(frac) or frac < 0.0 or frac > 1.0:
        return None
    return _valid(frac * 100.0)


def _from_json(json_text: str) -> float | None:
    """coverage.py JSON: ``["totals"]["percent_covered"]`` (already 0-100)."""
    if not _artifact_within_limit(json_text):
        return None
    try:
        data = json.loads(json_text)
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    totals = data.get("totals")
    if not isinstance(totals, dict) or "percent_covered" not in totals:
        return None
    return _valid(totals.get("percent_covered"))


def _from_stdout(stdout: str) -> float | None:
    """coverage.py ``TOTAL ... 96%`` or pytest-cov ``Total coverage: 87.34%``. Last match wins."""
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
    stdout: str,
    xml_text: str | None = None,
    json_text: str | None = None,
) -> float | None:
    """Return the overall line-coverage percentage (0.0-100.0), or ``None`` if it cannot be
    confidently determined. PURE, FAIL-CLOSED, never raises.

    Sources are consulted in priority order; the first that yields a valid number wins (we do
    NOT average disagreeing sources): coverage.xml (Cobertura ``line-rate``), then coverage.json
    (``totals.percent_covered``), then the terminal summary in ``stdout``. ``None`` means
    "coverage unknown" -> the DoD ``coverage`` dimension HOLDs.
    """
    for source in (
        (_from_xml, xml_text),
        (_from_json, json_text),
        (_from_stdout, stdout),
    ):
        fn, text = source
        if isinstance(text, str) and text:
            pct = fn(text)
            if pct is not None:
                return pct
    return None
