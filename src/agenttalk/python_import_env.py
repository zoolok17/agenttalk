"""Deterministic CPython import environment for the pinned control plane.

The ambient environment channels that can redirect a Windows CPython import
are kept as one audited class here.  ``PYTHONPATH`` and ``PYTHONHOME`` change
the core path configuration; ``PYTHONUSERBASE`` selects a user site;
``PYTHONPLATLIBDIR`` changes platform-library discovery; and ``PYTHONCASEOK``
changes Windows import-name matching.  ``PYTHONNOUSERSITE`` closes both an
explicit user base and the user site derived indirectly from ``APPDATA`` or
the Windows profile variables (``USERPROFILE`` or
``HOMEDRIVE``/``HOMEPATH``), while
``PYTHONSAFEPATH`` closes the working-directory/script-directory prefix on
Python 3.11+.

Executable-adjacent ``._pth``/``pyvenv.cfg`` files and packages installed in
the pinned runtime remain part of that runtime's trust anchor, not ambient
environment channels.
"""

from __future__ import annotations

from collections.abc import MutableMapping


PINNED_PYTHON_ENV_CLEAR = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "PYTHONPLATLIBDIR",
    "PYTHONCASEOK",
)
PINNED_PYTHON_ENV_FORCE = (
    ("PYTHONNOUSERSITE", "1"),
    ("PYTHONSAFEPATH", "1"),
)


def isolate_pinned_python_environment(env: MutableMapping[str, str]) -> None:
    """Apply the complete ambient import-origin policy in place.

    Matching is case-insensitive so a Windows environment copied into a plain
    ``dict`` cannot retain a differently-cased alias beside the managed key.
    """
    managed = {name.casefold() for name in PINNED_PYTHON_ENV_CLEAR}
    managed.update(
        name.casefold() for name, _value in PINNED_PYTHON_ENV_FORCE
    )
    for name in list(env):
        if name.casefold() in managed:
            env.pop(name, None)
    env.update(PINNED_PYTHON_ENV_FORCE)
