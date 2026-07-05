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
    "hexagon-analyst": "hexagon-analyst.png",
    "hexagon-architect": "hexagon-architect.png",
    "hexagon-builder": "hexagon-builder.png",
    "hexagon-detective": "hexagon-detective.png",
    "hexagon-devops": "hexagon-devops.png",
    "hexagon-docs": "hexagon-docs.png",
    "hexagon-monitor": "hexagon-monitor.png",
    "hexagon-sandbox": "hexagon-sandbox.png",
    "hexagon-social": "hexagon-social.png",
    "hexagon-translator": "hexagon-translator.png",
    "oval-muted-architect": "oval-muted-architect.png",
    "oval-muted-dataflow": "oval-muted-dataflow.png",
    "oval-muted-debugger": "oval-muted-debugger.png",
    "oval-muted-devops": "oval-muted-devops.png",
    "oval-muted-docs": "oval-muted-docs.png",
    "oval-muted-performance": "oval-muted-performance.png",
    "oval-muted-planner": "oval-muted-planner.png",
    "oval-muted-qa": "oval-muted-qa.png",
    "oval-muted-security": "oval-muted-security.png",
    "oval-muted-writer": "oval-muted-writer.png",
    "oval-vivid-analytics": "oval-vivid-analytics.png",
    "oval-vivid-database": "oval-vivid-database.png",
    "oval-vivid-delivery": "oval-vivid-delivery.png",
    "oval-vivid-designer": "oval-vivid-designer.png",
    "oval-vivid-hacker": "oval-vivid-hacker.png",
    "oval-vivid-infra": "oval-vivid-infra.png",
    "oval-vivid-ml": "oval-vivid-ml.png",
    "oval-vivid-mobile": "oval-vivid-mobile.png",
    "oval-vivid-security": "oval-vivid-security.png",
    "oval-vivid-web": "oval-vivid-web.png",
    "rounded-square-accessibility": "rounded-square-accessibility.png",
    "rounded-square-architect": "rounded-square-architect.png",
    "rounded-square-debugger": "rounded-square-debugger.png",
    "rounded-square-detective": "rounded-square-detective.png",
    "rounded-square-launch": "rounded-square-launch.png",
    "rounded-square-mobile": "rounded-square-mobile.png",
    "rounded-square-network": "rounded-square-network.png",
    "rounded-square-reader": "rounded-square-reader.png",
    "rounded-square-terminal": "rounded-square-terminal.png",
    "rounded-square-writer": "rounded-square-writer.png",
    "star-analytics": "star-analytics.png",
    "star-cloud": "star-cloud.png",
    "star-database": "star-database.png",
    "star-designer": "star-designer.png",
    "star-devops": "star-devops.png",
    "star-integrator": "star-integrator.png",
    "star-reviewer": "star-reviewer.png",
    "star-support": "star-support.png",
    "star-web": "star-web.png",
    "star-workflow": "star-workflow.png",
    "triangle-analytics": "triangle-analytics.png",
    "triangle-database": "triangle-database.png",
    "triangle-datagraph": "triangle-datagraph.png",
    "triangle-delivery": "triangle-delivery.png",
    "triangle-network": "triangle-network.png",
    "triangle-performance": "triangle-performance.png",
    "triangle-privacy": "triangle-privacy.png",
    "triangle-reviewer": "triangle-reviewer.png",
    "triangle-search": "triangle-search.png",
    "triangle-security": "triangle-security.png",
}

AVATAR_SHAPES: dict[str, str] = {
    "hexagon-analyst": "hexagon",
    "hexagon-architect": "hexagon",
    "hexagon-builder": "hexagon",
    "hexagon-detective": "hexagon",
    "hexagon-devops": "hexagon",
    "hexagon-docs": "hexagon",
    "hexagon-monitor": "hexagon",
    "hexagon-sandbox": "hexagon",
    "hexagon-social": "hexagon",
    "hexagon-translator": "hexagon",
    "oval-muted-architect": "oval-muted",
    "oval-muted-dataflow": "oval-muted",
    "oval-muted-debugger": "oval-muted",
    "oval-muted-devops": "oval-muted",
    "oval-muted-docs": "oval-muted",
    "oval-muted-performance": "oval-muted",
    "oval-muted-planner": "oval-muted",
    "oval-muted-qa": "oval-muted",
    "oval-muted-security": "oval-muted",
    "oval-muted-writer": "oval-muted",
    "oval-vivid-analytics": "oval-vivid",
    "oval-vivid-database": "oval-vivid",
    "oval-vivid-delivery": "oval-vivid",
    "oval-vivid-designer": "oval-vivid",
    "oval-vivid-hacker": "oval-vivid",
    "oval-vivid-infra": "oval-vivid",
    "oval-vivid-ml": "oval-vivid",
    "oval-vivid-mobile": "oval-vivid",
    "oval-vivid-security": "oval-vivid",
    "oval-vivid-web": "oval-vivid",
    "rounded-square-accessibility": "rounded-square",
    "rounded-square-architect": "rounded-square",
    "rounded-square-debugger": "rounded-square",
    "rounded-square-detective": "rounded-square",
    "rounded-square-launch": "rounded-square",
    "rounded-square-mobile": "rounded-square",
    "rounded-square-network": "rounded-square",
    "rounded-square-reader": "rounded-square",
    "rounded-square-terminal": "rounded-square",
    "rounded-square-writer": "rounded-square",
    "star-analytics": "star",
    "star-cloud": "star",
    "star-database": "star",
    "star-designer": "star",
    "star-devops": "star",
    "star-integrator": "star",
    "star-reviewer": "star",
    "star-support": "star",
    "star-web": "star",
    "star-workflow": "star",
    "triangle-analytics": "triangle",
    "triangle-database": "triangle",
    "triangle-datagraph": "triangle",
    "triangle-delivery": "triangle",
    "triangle-network": "triangle",
    "triangle-performance": "triangle",
    "triangle-privacy": "triangle",
    "triangle-reviewer": "triangle",
    "triangle-search": "triangle",
    "triangle-security": "triangle",
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
    return [{"id": avatar_id, "file": file, "shape": AVATAR_SHAPES.get(avatar_id, "")}
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
    return {
        "id": avatar_id,
        "file": AVATAR_ASSETS[avatar_id],
        "source": source,
        "shape": AVATAR_SHAPES.get(avatar_id, ""),
    }


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
