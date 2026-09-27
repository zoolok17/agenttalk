"""Single closed dispatch table for acceptance plan/route capabilities."""

from typing import NamedTuple


class Capabilities(NamedTuple):
    version: int
    modern: bool
    cold: bool
    preflight: bool


_KNOWN = {1: Capabilities(1, False, False, False), 2: Capabilities(2, True, False, False),
          3: Capabilities(3, True, True, False), 4: Capabilities(4, True, True, True)}


def capabilities(version):
    """Reject unknown or non-integer versions before choosing any legacy behavior."""
    if type(version) is not int or version not in _KNOWN:
        from agenttalk.acceptance import AcceptanceError
        raise AcceptanceError("acceptance_policy_invalid", "unsupported acceptance schema version")
    return _KNOWN[version]
