"""#55 slice-1 static comprehension plane (DESIGN-55-comprehension-plane.md).

Local-only, advisory, immutable-generation inventory of one legacy-repository
snapshot under ``.agenttalk/comprehension/``. This package contains no network
code path (core invariant 1) — see the design doc's "Privacy and offline
enforcement" section before adding any import here.
"""

from __future__ import annotations
