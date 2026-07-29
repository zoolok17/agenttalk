"""Shared producer/consumer contract for coverage evidence gates."""

from __future__ import annotations


ASSURANCE_PROFILES = ("change", "release", "deep")
_GATE_BY_PROFILE = {
    profile: f"coverage:{profile}"
    for profile in ASSURANCE_PROFILES
}
COVERAGE_GATE_NAMES = frozenset(_GATE_BY_PROFILE.values())
_PROFILE_BY_GATE = {gate: profile for profile, gate in _GATE_BY_PROFILE.items()}


def coverage_gate_name(profile: str) -> str:
    """Return the only coverage gate emitted by an assurance scan profile."""
    try:
        return _GATE_BY_PROFILE[profile]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"coverage profile must be one of {', '.join(ASSURANCE_PROFILES)}"
        ) from exc


def coverage_profile_from_gate(gate: str) -> str:
    """Return the assurance profile that can emit ``gate``."""
    try:
        return _PROFILE_BY_GATE[gate]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"coverage gate must be one of {', '.join(sorted(COVERAGE_GATE_NAMES))}"
        ) from exc
