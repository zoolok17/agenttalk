"""Health check command: did the user wire everything up correctly?

`agenttalk doctor` runs a series of small checks and reports
each as ``ok`` / ``warn`` / ``error``, with a remediation hint when
something is off. Designed so a fresh user can self-diagnose
"why isn't the bus working?" without reading the code.
"""

from __future__ import annotations

import filecmp
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import __version__
from agenttalk import codex_config as cxc
from agenttalk import install_skills as iskl
from agenttalk import signing as _signing
from agenttalk.store import Store, find_root


@dataclass
class Check:
    name: str
    status: str          # "ok" | "warn" | "error"
    details: str = ""
    fix: str = ""        # one-line remediation hint, optional


@dataclass
class Report:
    agenttalk_version: str
    python_version: str
    project_root: str
    checks: list[Check] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(c.status == "error" for c in self.checks):
            return "error"
        if any(c.status == "warn" for c in self.checks):
            return "warn"
        return "ok"

    def to_dict(self) -> dict:
        return {
            "agenttalk_version": self.agenttalk_version,
            "python_version": self.python_version,
            "project_root": self.project_root,
            "overall": self.overall,
            "checks": [
                {"name": c.name, "status": c.status,
                 "details": c.details, "fix": c.fix or None}
                for c in self.checks
            ],
        }


def run(project_root: Path | None = None) -> Report:
    """Run every check and return a structured report.

    Both ``agenttalk doctor`` and ``agenttalk doctor --json`` consume
    the same Report; rendering is the caller's job.
    """
    root = (project_root or find_root()).resolve()
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    report = Report(
        agenttalk_version=__version__,
        python_version=py,
        project_root=str(root),
    )

    store = Store(root)
    report.checks.append(_check_init(store))
    if store.initialized():
        report.checks.extend(_check_skills())
        report.checks.append(_check_codex_config(root))
        report.checks.append(_check_hmac(store, root))
        report.checks.extend(_check_heartbeats(store))
    return report


# ---------------------------------------------------------- individual checks

def _check_init(store: Store) -> Check:
    if not store.initialized():
        return Check(
            name="store.initialized",
            status="error",
            details=f"no .agenttalk/ found at {store.root}",
            fix="run `agenttalk init --here --agents claude,codex` from the project root",
        )
    try:
        cfg = store.load_config()
    except (ValueError, OSError) as e:
        return Check(
            name="store.initialized",
            status="error",
            details=str(e),
            fix="re-init with `agenttalk init --here --agents <name>,<name> --force`",
        )
    agents = cfg.get("agents", [])
    return Check(
        name="store.initialized",
        status="ok",
        details=f"agents: {', '.join(agents)} · session_id: {cfg.get('session_id')}",
    )


def _check_skills() -> list[Check]:
    """Compare bundled skill files against the global install locations.

    Both Claude commands and Codex skills need to be installed and
    in sync with the bundled package for `/agenttalk.*` skills to
    work as documented.
    """
    out: list[Check] = []
    for label, side, target in (
        ("claude_skills", "claude", iskl.default_claude_dir()),
        ("codex_skills", "codex", iskl.default_codex_dir()),
    ):
        out.append(_compare_skill_side(label, side, target))
    return out


def _compare_skill_side(name: str, side: str, target: Path) -> Check:
    """Check that all bundled <side> skill files exist and match the install."""
    pairs = iskl._claude_pairs(target) if side == "claude" else iskl._codex_pairs(target)
    if not pairs:
        return Check(
            name=name,
            status="error",
            details=f"no bundled {side} skills found in package",
            fix="reinstall agenttalk",
        )
    missing: list[str] = []
    differs: list[str] = []
    for src, dst in pairs:
        if not dst.exists():
            missing.append(dst.name)
            continue
        try:
            if not filecmp.cmp(src, dst, shallow=False):
                differs.append(dst.name)
        except OSError:
            differs.append(dst.name)
    total = len(pairs)
    if missing:
        return Check(
            name=name,
            status="error",
            details=f"{len(missing)}/{total} missing under {target}",
            fix=f"run `agenttalk install-skills{' --claude-only' if side == 'claude' else ' --codex-only'}`",
        )
    if differs:
        return Check(
            name=name,
            status="warn",
            details=f"{len(differs)}/{total} differ from bundled version under {target}",
            fix="run `agenttalk install-skills --force` to overwrite (preserves nothing)",
        )
    return Check(
        name=name,
        status="ok",
        details=f"{total}/{total} in sync at {target}",
    )


def _check_codex_config(project_root: Path) -> Check:
    """Look at ~/.codex/config.toml for the per-project sandbox block."""
    try:
        st = cxc.status(cxc.default_config_path(), project_root)
    except OSError as e:
        return Check(
            name="codex_config",
            status="warn",
            details=f"could not read {cxc.default_config_path()}: {e}",
        )
    if not st["config_exists"]:
        return Check(
            name="codex_config",
            status="warn",
            details="no ~/.codex/config.toml — Codex is likely not installed on this machine",
        )
    if not st["section_present"]:
        return Check(
            name="codex_config",
            status="warn",
            details=f"no per-project block for {st['project_dir']}",
            fix="run `agenttalk codex-config --enable` to let Codex call agenttalk from its sandbox",
        )
    keys = st["keys"]
    missing_keys = [k for k, v in keys.items() if v is None]
    if missing_keys:
        return Check(
            name="codex_config",
            status="warn",
            details=f"per-project block exists but missing: {', '.join(missing_keys)}",
            fix="run `agenttalk codex-config --enable` to set them",
        )
    return Check(
        name="codex_config",
        status="ok",
        details="per-project block present with all three managed keys",
    )


def _check_hmac(store: Store, project_root: Path) -> Check:
    """Verify HMAC signing setup.

    Both the project_id (path-derived) and the enforcement signal
    (per-user key file's existence) live OUTSIDE attacker-writable
    ``.agenttalk/`` — neither can be tampered with via config edits.
    If the key file exists for this project's path-derived ID, this
    check reports its health; otherwise signing is reported as
    disabled.
    """
    project_id = store.project_id()  # always returns (path-derived)
    health = _signing.inspect_key(project_id, project_root)
    if not health.exists:
        return Check(
            name="hmac", status="ok",
            details=f"disabled (no key file at {health.path}; run `agenttalk hmac-init` to enable)",
        )
    if not health.readable:
        return Check(
            name="hmac", status="error",
            details=f"key file at {health.path} is not readable by this process",
        )
    if health.in_project_dir:
        return Check(
            name="hmac", status="error",
            details=f"key file is INSIDE the project at {health.path}",
            fix="move it under the per-user keys dir (defeats the threat model otherwise)",
        )
    if health.mode_warning:
        return Check(
            name="hmac", status="warn",
            details=health.mode_warning,
        )
    return Check(
        name="hmac", status="ok",
        details=f"enabled · key at {health.path}",
    )


def _check_heartbeats(store: Store) -> list[Check]:
    """One check per agent — is anyone actually listening right now?"""
    cfg = store.load_config()
    now = datetime.now(timezone.utc)
    out: list[Check] = []
    for a in cfg.get("agents", []):
        hb = store.read_heartbeat(a)
        if hb is None:
            out.append(Check(
                name=f"heartbeat.{a}",
                status="warn",
                details="no heartbeat — agent has never run `agenttalk wait`",
            ))
            continue
        age = (now - hb).total_seconds()
        if age > 300:  # 5 min
            out.append(Check(
                name=f"heartbeat.{a}",
                status="warn",
                details=f"stale (last seen {int(age)}s ago); peer probably not listening",
            ))
        else:
            out.append(Check(
                name=f"heartbeat.{a}",
                status="ok",
                details=f"last seen {int(age)}s ago",
            ))
    return out
