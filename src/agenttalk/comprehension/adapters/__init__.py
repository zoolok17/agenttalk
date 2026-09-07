"""Bundled, in-process, deterministic source adapters (DESIGN-55-
comprehension-plane.md, "System boundary": "Adapters run in-process inside
that worker and receive only an allowlisted input object."). Slice 1 ships
exactly one: :mod:`.java` (approved PR-B plan, C-5: Java-only this slice).
"""

from __future__ import annotations
