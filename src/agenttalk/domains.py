"""Pure domain registry core for the native middle tier.

The registry is durable project coordination data, not active bus state.
It deliberately has no live-git dependency: callers pass repo-relative paths
as data, and later lane/knowledge layers can stamp the returned registry hash.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenttalk.store import validate_agent_name, validate_group_name


FILENAME = "domains.json"
SCHEMA_VERSION = 1

SHARED_CATEGORIES = frozenset({
    "lock",
    "package-metadata",
    "schema",
    "migration",
    "generated",
    "ci-config",
    "public-api",
    "release-version",
})

SHARED_REQUIRES = frozenset({
    "shared-lease",
    "lead-approval",
    "shared-lease-or-lead-approval",
})

_DOMAIN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"\A[A-Za-z]:")
_REFSET_KEYS = frozenset({"agents", "groups", "roles"})


class DomainError(ValueError):
    """Raised for an invalid domain registry or path/glob input."""


@dataclass(frozen=True)
class Registry:
    path: Path
    data: dict[str, Any]
    registry_hash: str
    source_exists: bool


def empty_registry() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "domains": {}, "shared_paths": []}


def default_casefold_paths() -> bool:
    """Best-effort default for path matching on the local workstation."""
    return os.name == "nt"


def load_registry(path: Path, cfg: dict[str, Any]) -> Registry:
    """Load, validate, and normalize ``.agenttalk/domains.json``.

    A missing file is a valid empty registry so Phase 0 can land before any
    project has declared domains. Malformed or semantically invalid files fail
    closed with context.
    """
    source_exists = path.exists()
    if not source_exists:
        data = empty_registry()
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise DomainError(f"domain registry {path} is not valid JSON: {e}") from e
        except OSError as e:
            raise DomainError(f"cannot read domain registry {path}: {e}") from e
        data = validate_registry(raw, cfg)
    return Registry(path=path, data=data, registry_hash=registry_hash(data),
                    source_exists=source_exists)


def validate_registry(raw: object, cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DomainError("domain registry must be a JSON object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise DomainError(
            f"domain registry schema_version must be {SCHEMA_VERSION}, got {version!r}"
        )
    domains_raw = raw.get("domains")
    if not isinstance(domains_raw, dict):
        raise DomainError("domain registry 'domains' must be an object")
    shared_raw = raw.get("shared_paths", [])
    if not isinstance(shared_raw, list):
        raise DomainError("domain registry 'shared_paths' must be a list")

    seen_ids: dict[str, str] = {}
    domains: dict[str, Any] = {}
    for domain_id, entry in domains_raw.items():
        domain_id = _validate_domain_id(domain_id)
        key = domain_id.casefold()
        if key in seen_ids:
            raise DomainError(
                f"domain ids {seen_ids[key]!r} and {domain_id!r} only differ by case"
            )
        seen_ids[key] = domain_id
        domains[domain_id] = _validate_domain(domain_id, entry, cfg)

    shared_paths = [
        _validate_shared_path(i, entry, cfg) for i, entry in enumerate(shared_raw)
    ]
    # Reject DUPLICATE normalized globs: two shared_paths entries for the identical glob
    # with different approver sets are ambiguous-by-construction, and the lane verdict
    # keys matching entries by glob - a duplicate would collapse the all-matching rule
    # (D-11) to one approver set, silently bypassing the other entry's authority (codex
    # P1). Fail closed: merge them into one entry with the combined approvers instead.
    seen_globs: dict[str, int] = {}
    for i, entry in enumerate(shared_paths):
        g = entry["glob"]
        if g in seen_globs:
            raise DomainError(
                f"shared_paths[{i}].glob {g!r} duplicates shared_paths[{seen_globs[g]}]; "
                "merge entries with the same glob into one (combine their approvers/reviewers)"
            )
        seen_globs[g] = i
    return {
        "schema_version": SCHEMA_VERSION,
        "domains": domains,
        "shared_paths": shared_paths,
    }


def registry_hash(registry: dict[str, Any]) -> str:
    blob = json.dumps(
        registry, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def resolve_refset(refset: dict[str, list[str]], cfg: dict[str, Any]) -> list[str]:
    """Resolve agent/group/role refs to concrete active agents in roster order."""
    roster = list(cfg.get("agents", []) or [])
    groups = cfg.get("groups", {}) or {}
    roles = cfg.get("roles", {}) or {}
    wanted = set(refset.get("agents", []))
    for group in refset.get("groups", []):
        wanted.update(groups.get(group, []))
    role_names = set(refset.get("roles", []))
    out: list[str] = []
    for agent in roster:
        if agent in wanted or roles.get(agent) in role_names:
            out.append(agent)
    return out


def normalize_repo_path(path: str, *, casefold: bool = False) -> str:
    return _normalize_repoish(path, label="path", casefold=casefold)


def normalize_glob(pattern: str, *, casefold: bool = False) -> str:
    return _normalize_repoish(pattern, label="glob", casefold=casefold)


def glob_matches(pattern: str, path: str, *, casefold: bool = False) -> bool:
    pat = normalize_glob(pattern, casefold=casefold).split("/")
    target = normalize_repo_path(path, casefold=casefold).split("/")
    return _match_segments(pat, target)


def check_path(registry: dict[str, Any], path: str, *,
               casefold_paths: bool | None = None) -> dict[str, Any]:
    casefold = default_casefold_paths() if casefold_paths is None else casefold_paths
    display_path = normalize_repo_path(path)
    domains: list[str] = []
    for domain_id, domain in registry.get("domains", {}).items():
        if any(glob_matches(glob, display_path, casefold=casefold)
               for glob in domain["owned_globs"]):
            domains.append(domain_id)
    shared_matches: list[dict[str, Any]] = []
    for entry in registry.get("shared_paths", []):
        if glob_matches(entry["glob"], display_path, casefold=casefold):
            shared_matches.append({
                "glob": entry["glob"],
                "category": entry["category"],
                "requires": entry["requires"],
            })
    return {
        "path": display_path,
        "domains": domains,
        "shared_paths": shared_matches,
        "owned": bool(domains),
        "shared": bool(shared_matches),
        "unowned": not domains,
        "overlap": len(domains) > 1,
        "casefold_paths": casefold,
    }


def check_paths(registry: dict[str, Any], paths: list[str], *,
                casefold_paths: bool | None = None) -> list[dict[str, Any]]:
    return [
        check_path(registry, path, casefold_paths=casefold_paths)
        for path in paths
    ]


def _validate_domain_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DomainError("domain id must be a non-empty string")
    if not _DOMAIN_ID_RE.match(value):
        raise DomainError(
            f"domain id {value!r} is not a safe identifier "
            "(allowed: alphanumeric plus . _ -, must start with a letter "
            "or digit, max 64 chars)"
        )
    return value


def _validate_domain(domain_id: str, entry: object, cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise DomainError(f"domain {domain_id!r} must be an object")
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DomainError(f"domain {domain_id!r} title must be a non-empty string")
    owned_globs = _validate_glob_list(entry.get("owned_globs"), f"domain {domain_id!r} owned_globs")
    domain = {
        "title": title,
        "owners": _validate_refset(entry.get("owners"), "owners", cfg, required=True),
        "reviewers": _validate_refset(entry.get("reviewers"), "reviewers", cfg),
        "curators": _validate_refset(entry.get("curators"), "curators", cfg),
        "owned_globs": owned_globs,
    }
    if "description" in entry:
        desc = entry["description"]
        if not isinstance(desc, str):
            raise DomainError(f"domain {domain_id!r} description must be a string")
        domain["description"] = desc
    if "metadata" in entry:
        metadata = entry["metadata"]
        if not isinstance(metadata, dict):
            raise DomainError(f"domain {domain_id!r} metadata must be an object")
        if not all(isinstance(k, str) for k in metadata):
            raise DomainError(f"domain {domain_id!r} metadata keys must be strings")
        domain["metadata"] = metadata
    return domain


def _validate_shared_path(index: int, entry: object, cfg: dict[str, Any]) -> dict[str, Any]:
    label = f"shared_paths[{index}]"
    if not isinstance(entry, dict):
        raise DomainError(f"{label} must be an object")
    glob = entry.get("glob")
    if not isinstance(glob, str):
        raise DomainError(f"{label}.glob must be a string")
    category = entry.get("category")
    if category not in SHARED_CATEGORIES:
        raise DomainError(
            f"{label}.category must be one of {sorted(SHARED_CATEGORIES)}, got {category!r}"
        )
    requires = entry.get("requires")
    if requires not in SHARED_REQUIRES:
        raise DomainError(
            f"{label}.requires must be one of {sorted(SHARED_REQUIRES)}, got {requires!r}"
        )
    out = {
        "glob": normalize_glob(glob),
        "category": category,
        "requires": requires,
        "default_reviewers": _validate_refset(
            entry.get("default_reviewers"), "default_reviewers", cfg,
        ),
        "default_approvers": _validate_refset(
            entry.get("default_approvers"), "default_approvers", cfg,
        ),
    }
    if "description" in entry:
        desc = entry["description"]
        if not isinstance(desc, str):
            raise DomainError(f"{label}.description must be a string")
        out["description"] = desc
    return out


def _validate_refset(value: object, field: str, cfg: dict[str, Any], *,
                     required: bool = False) -> dict[str, list[str]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DomainError(f"{field} must be an object with agents/groups/roles lists")
    unknown = set(value) - _REFSET_KEYS
    if unknown:
        raise DomainError(f"{field} has unknown keys {sorted(unknown)}")
    refs = {
        "agents": _validate_string_list(value.get("agents", []), f"{field}.agents"),
        "groups": _validate_string_list(value.get("groups", []), f"{field}.groups"),
        "roles": _validate_string_list(value.get("roles", []), f"{field}.roles"),
    }
    cfg_agents = set(cfg.get("agents", []) or [])
    cfg_groups = cfg.get("groups", {}) or {}
    cfg_roles = cfg.get("roles", {}) or {}
    known_roles = {r for r in cfg_roles.values() if isinstance(r, str)}

    for agent in refs["agents"]:
        validate_agent_name(agent)
        if agent not in cfg_agents:
            raise DomainError(f"{field}.agents references unknown agent {agent!r}")
    for group in refs["groups"]:
        validate_group_name(group)
        if group not in cfg_groups:
            raise DomainError(f"{field}.groups references unknown group {group!r}")
    for role in refs["roles"]:
        if not isinstance(role, str) or not role or len(role) > 64 or not role.isprintable():
            raise DomainError(f"{field}.roles entries must be printable strings of at most 64 chars")
        if role not in known_roles:
            raise DomainError(f"{field}.roles references unknown role {role!r}")
    refs = {k: list(dict.fromkeys(v)) for k, v in refs.items()}
    if required and not any(refs.values()):
        raise DomainError(f"{field} must reference at least one agent, group, or role")
    return refs


def _validate_glob_list(value: object, label: str) -> list[str]:
    globs = _validate_string_list(value, label)
    if not globs:
        raise DomainError(f"{label} must contain at least one glob")
    return [normalize_glob(glob) for glob in globs]


def _validate_string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise DomainError(f"{label} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise DomainError(f"{label} entries must be non-empty strings")
        out.append(item)
    return out


def _normalize_repoish(value: str, *, label: str, casefold: bool) -> str:
    if not isinstance(value, str) or not value:
        raise DomainError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise DomainError(f"{label} must not contain NUL bytes")
    text = value.replace("\\", "/").strip()
    if not text:
        raise DomainError(f"{label} must be a non-empty string")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text):
        raise DomainError(f"{label} {value!r} must be repo-relative")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise DomainError(f"{label} {value!r} escapes the repository")
            parts.pop()
            continue
        parts.append(part.casefold() if casefold else part)
    if not parts:
        raise DomainError(f"{label} {value!r} does not name a repo-relative path")
    return "/".join(parts)


def _match_segments(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        return _match_segments(pattern[1:], path) or (
            bool(path) and _match_segments(pattern, path[1:])
        )
    if not path:
        return False
    if fnmatch.fnmatchcase(path[0], head):
        return _match_segments(pattern[1:], path[1:])
    return False
