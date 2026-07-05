"""Allowlisted display-avatar resolution.

Avatar choices are cosmetic preferences stored in config.json. They never name
files directly: every emitted image filename comes from AVATAR_ASSETS.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

OPERATOR_PRINCIPAL = "operator"
OPERATOR_DEFAULT_ID = "operator"
RESERVED_PRINCIPALS = frozenset({OPERATOR_PRINCIPAL})

AVATAR_ASSETS: dict[str, str] = {
    "claude-arch": "claude-arch.png",
    "claude-dev": "claude-dev.png",
    "claude-docs": "claude-docs.png",
    "claude-lead": "claude-lead.png",
    "claude-rev": "claude-rev.png",
    "codex-dev": "codex-dev.png",
    "codex-infra": "codex-infra.png",
    "codex-rev": "codex-rev.png",
    "codex-scout": "codex-scout.png",
    "codex-test": "codex-test.png",
    "operator": "operator.png",
}

ROLE_DEFAULTS: dict[tuple[str, str], str] = {
    ("claude", "arch"): "claude-arch",
    ("claude", "dev"): "claude-dev",
    ("claude", "docs"): "claude-docs",
    ("claude", "lead"): "claude-lead",
    ("claude", "rev"): "claude-rev",
    ("codex", "dev"): "codex-dev",
    ("codex", "infra"): "codex-infra",
    ("codex", "rev"): "codex-rev",
    ("codex", "scout"): "codex-scout",
    ("codex", "test"): "codex-test",
}

_ROLE_ALIASES = {
    "architect": "arch",
    "arch": "arch",
    "developer": "dev",
    "dev": "dev",
    "documentation": "docs",
    "docs": "docs",
    "infrastructure": "infra",
    "infra": "infra",
    "lead": "lead",
    "reviewer": "rev",
    "rev": "rev",
    "scout": "scout",
    "tester": "test",
    "test": "test",
}
_AVATAR_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")


def normalize_avatar_id(value: object) -> str | None:
    """Return a canonical allowlisted avatar id, or None for any bad value."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if not _AVATAR_ID_RE.match(candidate):
        return None
    return candidate if candidate in AVATAR_ASSETS else None


def normalize_role(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _ROLE_ALIASES.get(value.strip().casefold())


def role_default_id(role: object, cli_family: object) -> str | None:
    role_key = normalize_role(role)
    if role_key is None or cli_family not in ("claude", "codex"):
        return None
    avatar_id = ROLE_DEFAULTS.get((str(cli_family), role_key))
    return avatar_id if avatar_id in AVATAR_ASSETS else None


def avatar_static_paths() -> tuple[str, ...]:
    return tuple(f"avatars/{file}" for file in AVATAR_ASSETS.values())


def available_avatars() -> list[dict[str, str]]:
    return [{"id": avatar_id, "file": file}
            for avatar_id, file in AVATAR_ASSETS.items()]


def sanitize_avatar_preferences(
    raw: object,
    principals: Iterable[str],
) -> tuple[dict[str, str], list[str]]:
    """Return render-safe avatar prefs and non-fatal warning strings.

    Bad data is display-only corruption, so callers should ignore the bad entry
    and keep rendering instead of failing load_config or /api/state.
    """
    if raw is None:
        return {}, []
    if not isinstance(raw, Mapping):
        return {}, ["avatars must be an object"]
    allowed = set(principals) | RESERVED_PRINCIPALS
    clean: dict[str, str] = {}
    warnings: list[str] = []
    for principal, value in raw.items():
        if not isinstance(principal, str) or principal not in allowed:
            warnings.append(f"ignored avatar preference for unknown principal {principal!r}")
            continue
        avatar_id = normalize_avatar_id(value)
        if avatar_id is None:
            warnings.append(f"ignored invalid avatar id for {principal!r}")
            continue
        clean[principal] = avatar_id
    return clean, warnings


def _record(avatar_id: str, source: str) -> dict[str, str]:
    return {"id": avatar_id, "file": AVATAR_ASSETS[avatar_id], "source": source}


def resolve_avatar(
    preferences: Mapping[str, Any] | None,
    principal: str,
    role: object = None,
    cli_family: object = None,
) -> dict[str, str]:
    """Resolve a principal to an allowlisted avatar record.

    The "none" result carries no file and should be omitted from state payloads.
    """
    if isinstance(preferences, Mapping):
        chosen = normalize_avatar_id(preferences.get(principal))
        if chosen is not None:
            return _record(chosen, "chosen")
    if principal == OPERATOR_PRINCIPAL and OPERATOR_DEFAULT_ID in AVATAR_ASSETS:
        return _record(OPERATOR_DEFAULT_ID, "operator_default")
    default_id = role_default_id(role, cli_family)
    if default_id is not None:
        return _record(default_id, "role_default")
    return {"id": "", "file": "", "source": "none"}
