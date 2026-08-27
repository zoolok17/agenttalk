"""Typed error hierarchy for the comprehension plane (#55 slice-1).

One base class per PR-A subsystem, mirroring the codebase's existing
typed-refusal convention (e.g. ``lifecycle_lock.LifecycleLockError``,
``domains.DomainError``): callers branch on type, not on message text, and
every message stays human-actionable.
"""

from __future__ import annotations


class ComprehensionError(RuntimeError):
    """Base class for every typed refusal raised by this package."""

    reason_code = "comprehension_error"


class EnvelopeError(ComprehensionError):
    """A JSON document failed strict envelope, schema, or path-safety checks."""

    reason_code = "comprehension_envelope_invalid"
