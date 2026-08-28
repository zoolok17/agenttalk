"""Health check command: did the user wire everything up correctly?

`agenttalk doctor` runs a series of small checks and reports
each as ``ok`` / ``warn`` / ``error``, with a remediation hint when
something is off. Designed so a fresh user can self-diagnose
"why isn't the bus working?" without reading the code.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
# subprocess is used ONLY for timeout-bounded diagnostic runtime probes; never shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import __version__
from agenttalk import codex_config as cxc
from agenttalk import install_skills as iskl
from agenttalk import signing as _signing
from agenttalk import supervisor as sup
from agenttalk import powershell_host as psh
from agenttalk import supervisor_lifecycle
from agenttalk.store import (
    STORE_SCHEMA_CAPABILITIES,
    Store,
    find_root,
    find_stores_upward,
)


_TASK_QUERY_SENTINEL = "AGENTTALK_TASK_QUERY_V1:"
_TASK_QUERY_COMMAND = (
    "$ErrorActionPreference='Stop';"
    "$items=@(Get-ScheduledTask -ErrorAction Stop | ForEach-Object {"
    "$actions=@($_.Actions | ForEach-Object { [ordered]@{"
    "execute=[string]$_.Execute;arguments=[string]$_.Arguments;"
    "working_directory=[string]$_.WorkingDirectory} });"
    "[ordered]@{name=[string]$_.TaskName;path=[string]$_.TaskPath;"
    "state=[string]$_.State;actions=$actions} });"
    "$out=[ordered]@{sentinel='agenttalk-task-query-v1';tasks=$items};"
    f"Write-Output ('{_TASK_QUERY_SENTINEL}'+($out|ConvertTo-Json -Compress -Depth 6))"
)
_TASK_FILE_ARGUMENT_RE = re.compile(
    r"(?:^|\s)-File\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
    re.IGNORECASE,
)


@dataclass
class Check:
    name: str
    status: str          # "ok" | "warn" | "error"
    details: str = ""
    fix: str = ""        # one-line remediation hint, optional
    data: dict | None = None  # optional structured payload (JSON only)


@dataclass
class Report:
    agenttalk_version: str
    python_version: str
    project_root: str
    # #37 Fix 2: discriminate the RUNNING code when two writers report the same
    # --version. module_path distinguishes a PYTHONPATH=src checkout from an
    # installed wheel; schema_capabilities names what the running store supports,
    # so a capability-deficient writer (the one that can corrupt the store's
    # ordering invariant) is visible by comparing agents' `doctor` output.
    agenttalk_module_path: str = ""
    store_schema_capabilities: list[str] = field(default_factory=list)
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
            # Root FIRST (0.14.0, #13): the wrong-root footgun is diagnosed
            # by reading exactly one key, on both output surfaces.
            "project_root": self.project_root,
            "agenttalk_version": self.agenttalk_version,
            "agenttalk_module_path": self.agenttalk_module_path,
            "store_schema_capabilities": self.store_schema_capabilities,
            "python_version": self.python_version,
            "overall": self.overall,
            "checks": [
                {"name": c.name, "status": c.status,
                 "details": c.details, "fix": c.fix or None,
                 "data": c.data}
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
    import agenttalk as _agenttalk_pkg
    report = Report(
        agenttalk_version=__version__,
        python_version=py,
        project_root=str(root),
        agenttalk_module_path=str(Path(_agenttalk_pkg.__file__).resolve().parent),
        store_schema_capabilities=list(STORE_SCHEMA_CAPABILITIES),
    )

    store = Store(root)
    init_check = _check_init(store)
    report.checks.append(init_check)
    # Multi-store detection runs UNCONDITIONALLY (not gated on an
    # initialized store): the split-brain layout (#13) is most dangerous
    # exactly when the user is confused about which store they're on —
    # including when the resolved root has no store at all.
    report.checks.append(_check_multi_store(root))
    # Source-content currency of the BUNDLED skill files - runs UNCONDITIONALLY (reads the
    # package's bundled skills, independent of any project store), distinct from the install-
    # freshness checks (_check_skills / _check_devkit).
    report.checks.append(_check_skill_currency())
    report.checks.append(_check_powershell_host(store))
    artifact_check = _check_powershell_artifacts(store)
    if artifact_check is not None:
        report.checks.append(artifact_check)
    # Config-dependent checks are gated on a LOADABLE config: `_check_init`
    # already reports a corrupt config as an `error` (e.g. an active∩retired
    # overlap, #19), so running these would just re-raise the same
    # ValueError and crash `doctor` instead of returning a report. The init
    # error IS the registry-hygiene finding for that case.
    if store.initialized() and init_check.status != "error":
        report.checks.append(_check_operator_facing(store))
        esc_target = _check_escalation_target(store)
        if esc_target is not None:  # additive: absent for solo/healthy rosters
            report.checks.append(esc_target)
        report.checks.append(_check_identity_registry(store))
        report.checks.append(_check_store_hygiene(store))
        deadman_config = _check_deadman_config_source(store)
        if deadman_config is not None:
            report.checks.append(deadman_config)
        report.checks.extend(_check_skills())
        report.checks.append(_check_devkit())
        report.checks.append(_check_codex_config(root))
        report.checks.append(_check_hmac(store, root))
        report.checks.extend(_check_heartbeats(store))
        waiters = _check_active_waiters(store)
        if waiters is not None:  # additive: absent unless a live waiter exists
            report.checks.append(waiters)
        coordination = _check_coordination_stalls(store)
        if coordination is not None:
            report.checks.append(coordination)
        codex_vis = _check_supervised_codex(store, runtime_checker=_check_agenttalk_runtime)
        if codex_vis is not None:  # additive: absent unless a supervised codex agent
            report.checks.append(codex_vis)
        supervisor_script = _check_supervisor_script_guard(store)
        if supervisor_script is not None:  # additive: absent unless stale/unreadable
            report.checks.append(supervisor_script)
        qwen_gateway = _check_ovh_qwen_gateway(store)
        if qwen_gateway is not None:
            report.checks.append(qwen_gateway)
        holds = _check_config_blocked_holds(store)
        if holds is not None:  # additive: absent unless a valid config-blocked hold exists
            report.checks.append(holds)
        report.checks.append(_check_detection_commit_gate(store))
        external_gate = _check_external_worker_commit_gate(store)
        if external_gate is not None:
            report.checks.append(external_gate)
        report.checks.append(_check_publication_order(store))
        kn_check = _check_knowledge(store)
        if kn_check is not None:  # additive: absent unless a knowledge store exists
            report.checks.append(kn_check)
        dl_check = _check_dead_letter(store)
        if dl_check is not None:  # additive: absent unless dead-letters exist
            report.checks.append(dl_check)
        ad_check = _check_attention_dispositions(store)
        if ad_check is not None:  # additive: absent unless torn disposition lines exist
            report.checks.append(ad_check)
        esc_check = _check_dead_letter_escalations(store)
        if esc_check is not None:  # additive: absent unless an unrouted backstop exists
            report.checks.append(esc_check)
        lead_check = _check_lead_unarmed(store)
        if lead_check is not None:  # additive: absent unless a lead-loop concern exists
            report.checks.append(lead_check)
    return report


def _check_publication_order(store: Store) -> Check:
    """Surface the publication-order sidecar's integrity state (#37).

    Serves the diagnostics the self-heal relies on: an absent tamper-anchor is a
    WARN (transient after a crash; the next send re-anchors), and a genuine
    corruption (digest mismatch / anchor-ahead / lone anchor) is an ERROR naming
    the cause — so an operator sees the state that a shared `--version` hides,
    rather than discovering it as a comms outage.
    """
    name = "publication order"
    order_path = store.state_dir / "message-publication-order.json"
    anchor_path = store.state_dir / "message-publication-order.anchor.json"
    if not order_path.exists():
        if anchor_path.exists():
            return Check(
                name=name, status="error",
                details="sidecar is missing while its tamper-anchor exists "
                        "(the durable order file was lost or removed)",
                fix="investigate; do not delete the anchor to work around this",
            )
        return Check(name=name, status="ok",
                     details="no durable order yet (legacy or empty store)")
    try:
        store._read_message_publication_order()
    except ValueError as exc:
        return Check(
            name=name, status="error",
            details=f"integrity check failed: {exc}",
            fix="investigate before writing; do not delete the sidecar/anchor blindly",
        )
    if not anchor_path.exists():
        return Check(
            name=name, status="warn",
            details="sidecar present but its tamper-evidence anchor is absent "
                    "(expected transiently after a crash; the next send re-anchors)",
            fix="run any send to re-anchor, or investigate if this persists",
        )
    return Check(name=name, status="ok", details="durable order and anchor present")


@dataclass(frozen=True)
class _SupervisorCommitGateAgent:
    launch_cwd: object
    policy_override: object
    has_policy_override: bool


def _supervisor_env_override(env: object, key: str) -> tuple[bool, object]:
    """Read a supervisor env override with Windows environment semantics."""
    if not isinstance(env, dict):
        return False, None
    wanted = key.casefold()
    found = False
    value: object = None
    for candidate, candidate_value in env.items():
        if isinstance(candidate, str) and candidate.casefold() == wanted:
            found = True
            value = candidate_value
    return found, value


def _supervisor_commit_gate_facts(
    store: Store,
    policy_env: str,
) -> tuple[set[str], dict[str, _SupervisorCommitGateAgent]]:
    """Best-effort external-worker identities and supervisor launch inputs."""
    external_workers: set[str] = set()
    supervisor_agents: dict[str, _SupervisorCommitGateAgent] = {}
    supervisor_path = store.dir / "supervisor.json"
    if supervisor_path.exists():
        try:
            supervisor = json.loads(supervisor_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            supervisor = None
        entries = supervisor.get("agents") if isinstance(supervisor, dict) else None
        if isinstance(entries, dict):
            for name, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                agent = str(name)
                if entry.get("trust_class") == "external-worker":
                    external_workers.add(agent)
                env = entry.get("env")
                has_policy_override, policy_override = _supervisor_env_override(
                    env,
                    policy_env,
                )
                supervisor_agents[agent] = _SupervisorCommitGateAgent(
                    launch_cwd=entry.get("cwd"),
                    policy_override=policy_override,
                    has_policy_override=has_policy_override,
                )
    return external_workers, supervisor_agents


def _commit_gate_policy_snapshot(
    configured: object,
    agent: str,
    *,
    project_root: Path | None = None,
    launch_cwd: object = None,
):
    """Resolve one operator policy path without letting diagnostics crash."""
    from agenttalk.wrapper.obligations import PolicySnapshot, ResolverState

    if configured is None or configured == "":
        return PolicySnapshot.inactive(agent=agent)
    try:
        if not isinstance(configured, str):
            raise TypeError("policy path must be a string")
        path = Path(configured).expanduser()
        if not path.is_absolute() and project_root is not None:
            if launch_cwd is None or launch_cwd == "":
                worker_cwd = project_root
            elif isinstance(launch_cwd, str):
                worker_cwd = Path(launch_cwd).expanduser()
                if not worker_cwd.is_absolute():
                    worker_cwd = project_root / worker_cwd
            else:
                raise TypeError("launch cwd must be a string")
            path = worker_cwd / path
        path = path.resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return PolicySnapshot(
            ResolverState.BLOCKED_POLICY,
            "unreadable",
            reason=f"operator policy path invalid: {type(exc).__name__}",
            agent=agent,
        )
    return PolicySnapshot.from_path(path, agent)


def _effective_commit_gate_policy(
    store: Store,
    agent: str,
    ambient_path: str | None,
    supervisor_agents: dict[str, _SupervisorCommitGateAgent],
):
    """Resolve the policy path from the environment the worker actually receives."""
    supervisor_agent = supervisor_agents.get(agent)
    if supervisor_agent is None:
        configured = ambient_path
        return configured, _commit_gate_policy_snapshot(configured, agent)
    configured = (
        supervisor_agent.policy_override
        if supervisor_agent.has_policy_override
        else ambient_path
    )
    return configured, _commit_gate_policy_snapshot(
        configured,
        agent,
        project_root=store.root,
        launch_cwd=supervisor_agent.launch_cwd,
    )


def _check_external_worker_commit_gate(store: Store) -> Check | None:
    """Warn when an external worker can use the legacy ungated commit path."""
    from agenttalk.wrapper.obligations import POLICY_ENV, ResolverState

    try:
        roster = store.load_config()
        roster_agents = {str(agent) for agent in roster.get("agents", []) or []}
        trust_classes = roster.get("trust_classes")
        if not isinstance(trust_classes, dict):
            trust_classes = {}
        roster_external = {
            str(agent)
            for agent, trust_class in trust_classes.items()
            if trust_class == "external-worker"
        }
    except (OSError, TypeError, ValueError):
        roster_agents = set()
        roster_external = set()
    supervisor_external, supervisor_agents = _supervisor_commit_gate_facts(
        store,
        POLICY_ENV,
    )
    external_workers = sorted(roster_external | supervisor_external)
    if not external_workers:
        return None

    process_path = os.environ.get(POLICY_ENV)
    ungated: list[str] = []
    missing_or_disabled: list[str] = []
    unusable: list[str] = []
    stronger_errors: list[str] = []
    rows: list[dict] = []
    for agent in external_workers:
        configured, policy = _effective_commit_gate_policy(
            store,
            agent,
            process_path,
            supervisor_agents,
        )
        if policy.status != ResolverState.ACTIVE:
            ungated.append(agent)
            if policy.status in {ResolverState.NOT_OWED, ResolverState.INACTIVE}:
                missing_or_disabled.append(agent)
            else:
                unusable.append(agent)
            if (
                policy.status == ResolverState.BLOCKED_POLICY
                and agent in roster_agents
            ):
                stronger_errors.append(agent)
        rows.append({
            "agent": agent,
            "policy_path": configured,
            "policy_status": policy.status.value,
            "grade": policy.grade,
            "reason": policy.reason,
        })

    if not ungated:
        return Check(
            name="external_worker_commit_gate",
            status="ok",
            details=(
                "external-worker commit-gate policy configured: "
                + ", ".join(external_workers)
            ),
            data={
                "external_workers": external_workers,
                "ungated_agents": [],
                "unusable_policy_agents": [],
                "stronger_error_agents": stronger_errors,
                "policies": rows,
            },
        )

    policy_statuses = {
        str(row["agent"]): str(row["policy_status"])
        for row in rows
    }

    def describe(agents: list[str]) -> str:
        return ", ".join(
            f"{agent} (policy_status={policy_statuses[agent]})"
            for agent in agents
        )

    detail_parts = []
    if missing_or_disabled:
        detail_parts.append(
            "external-worker agent(s) without enabled commit-gate policy: "
            + describe(missing_or_disabled)
        )
    if unusable:
        detail_parts.append(
            "external-worker agent(s) with present but unusable commit-gate policy: "
            + describe(unusable)
        )
    return Check(
        name="external_worker_commit_gate",
        status="warn",
        details="; ".join(detail_parts),
        fix=(
            f"configure or repair an operator-owned {POLICY_ENV} policy with "
            "an enabled detection-grade entry for each listed agent"
        ),
        data={
            "external_workers": external_workers,
            "ungated_agents": ungated,
            "unusable_policy_agents": unusable,
            "stronger_error_agents": stronger_errors,
            "policies": rows,
        },
    )


def _check_detection_commit_gate(store: Store) -> Check:
    """Report the operator launch-policy snapshot and durable breaker state."""
    from agenttalk.wrapper.obligations import (
        POLICY_ENV,
        DetectionCommitGate,
        ResolverState,
    )

    roster = store.load_config().get("agents", []) or []
    _, supervisor_agents = _supervisor_commit_gate_facts(store, POLICY_ENV)
    process_path = os.environ.get(POLICY_ENV)
    rows: list[dict] = []
    has_error = False
    has_warn = False
    has_legacy_broadcast = False
    for agent in roster:
        configured, policy = _effective_commit_gate_policy(
            store,
            agent,
            process_path,
            supervisor_agents,
        )
        gate = DetectionCommitGate(store, agent, policy, fence="doctor-read-only")
        status = gate.status()
        status["policy_path"] = configured
        rows.append(status)
        has_error = has_error or policy.status == ResolverState.BLOCKED_POLICY
        has_warn = has_warn or status.get("breaker", {}).get("tripped") is True
        legacy = status.get("legacy_broadcast", {})
        has_legacy_broadcast = has_legacy_broadcast or int(
            legacy.get("unenforced_total", 0)
        ) > 0
    has_warn = has_warn or has_legacy_broadcast
    details = "; ".join(
        f"{row['agent']}={row['status']}"
        + (
            f" legacy_unenforced={row['legacy_broadcast']['unenforced_total']}"
            if int(row.get("legacy_broadcast", {}).get("unenforced_total", 0)) > 0
            else ""
        )
        for row in rows
    )
    return Check(
        name="wrapped_commit_gate",
        status="error" if has_error else ("warn" if has_warn else "ok"),
        details=details or "no active agents",
        fix=(
            f"repair the operator-owned {POLICY_ENV} snapshot before dispatch"
            if has_error else (
                "audit and reset the tripped compliance breaker before paid dispatch"
                if any(
                    row.get("breaker", {}).get("tripped") is True for row in rows
                )
                else (
                    "audit legacy broadcast records; they were logged without "
                    "owed-action enforcement"
                    if has_legacy_broadcast else ""
                )
            )
        ),
        data={"agents": rows, "security_grade": False},
    )


def _check_ovh_qwen_gateway(store: Store) -> Check | None:
    path = store.dir / "supervisor.json"
    if not path.is_file():
        return None
    try:
        config = sup.load_supervisor_config(path)
    except (OSError, ValueError):
        return None
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    profile_agents = [
        name
        for name, spec in agents.items()
        if isinstance(spec, dict) and spec.get("backend_profile") == "ovh-qwen"
    ]
    if not profile_agents:
        return None
    from agenttalk import ovh_gateway_service as service

    try:
        status = service.gateway_status(store.root)
    except Exception as exc:  # noqa: BLE001 - doctor must report, never crash
        status = {
            "ready": False,
            "errors": [f"status_unavailable:{type(exc).__name__}"],
        }
    problems = list(status.get("errors") or [])
    config_trust = store.trust_classes()
    for name in profile_agents:
        spec = agents[name]
        if spec.get("trust_class") != "external-worker":
            problems.append(f"{name}:supervisor_trust_class")
        if config_trust.get(name) != "external-worker":
            problems.append(f"{name}:roster_trust_class")
    if any(os.environ.get(key) for key in ("OVH_KEY", "ANTHROPIC_API_KEY")):
        problems.append("supervisor_ambient_provider_key")
    problems = sorted(set(problems))
    return Check(
        name="ovh_qwen_gateway",
        status="error" if problems else "ok",
        details=(
            "gateway ready with constrained external-worker profile(s)"
            if not problems
            else "gateway/profile check failed: " + ", ".join(problems)
        ),
        fix=(
            "run `agenttalk gateway status`; remove ambient provider keys and repair "
            "the reported install/task/ledger/trust condition"
            if problems
            else ""
        ),
        data=status,
    )


def _check_powershell_host(store: Store) -> Check:
    if not sys.platform.startswith("win"):
        return Check(
            name="powershell_host",
            status="ok",
            details="N/A: generated PowerShell supervisor and Scheduled Tasks are Windows-only",
            data={"status": "n/a", "task_status": "n/a"},
        )
    try:
        record = supervisor_lifecycle.read_selected_host(store)
    except (OSError, supervisor_lifecycle.SupervisorLifecycleError) as exc:
        return Check(
            name="powershell_host",
            status="error",
            details=f"PowerShell Core host unavailable: {exc}; task status UNKNOWN",
            fix=psh.INSTALL_REMEDIATION,
            data={"status": "error", "task_status": "unknown"},
        )
    warning = record.get("_warning")
    public = psh.selection_public_view(record)
    task = _inspect_selected_task(store, record)
    public.update({
        "age_seconds": record.get("_age_seconds"),
        "warning": warning,
        "task_status": task["status"],
        "task": task,
    })
    task_error = task["status"] not in {"not_configured", "ok"}
    task_name = str(record.get("task_name") or "agenttalk-supervisor")
    fix = ""
    if task["status"] == "orphan":
        names = ", ".join(repr(item["name"]) for item in task.get("tasks", []))
        fix = (
            f"stop and uninstall orphan Scheduled Task binding(s) {names} before "
            "installing or starting the recorded binding"
        )
    elif task_error:
        fix = sup.task_recovery_remediation(store, str(record["path"]), task_name)
    elif warning:
        fix = f'agenttalk supervise --select-pwsh --pwsh "{record["path"]}"'
    return Check(
        name="powershell_host",
        status="error" if task_error else ("warn" if warning else "ok"),
        details=(
            f"{record['path']} ({record['source']}, {record['edition']} "
            f"{record['_version'].display}, age={record['_age_seconds']:.0f}s)"
            + (f"; {warning}" if warning else "")
            + (f"; task {task['status']}: {task.get('detail', '')}" if task_error else "")
        ),
        fix=fix,
        data=public,
    )


def _parse_task_query(stdout: str) -> list[dict]:
    rows = [
        line[len(_TASK_QUERY_SENTINEL):]
        for line in stdout.splitlines()
        if line.startswith(_TASK_QUERY_SENTINEL)
    ]
    if len(rows) != 1:
        raise ValueError("task query did not emit exactly one sentinel record")
    value = json.loads(rows[0])
    if not isinstance(value, dict) or set(value) != {"sentinel", "tasks"}:
        raise ValueError("task query schema mismatch")
    if value.get("sentinel") != "agenttalk-task-query-v1":
        raise ValueError("task query sentinel mismatch")
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("task query tasks must be an array")
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {"name", "path", "state", "actions"}:
            raise ValueError("task query task schema mismatch")
        if any(not isinstance(task.get(key), str) for key in ("name", "path", "state")):
            raise ValueError("task query task fields must be strings")
        actions = task.get("actions")
        if not isinstance(actions, list):
            raise ValueError("task query actions must be an array")
        for action in actions:
            if not isinstance(action, dict) or set(action) != {
                "execute", "arguments", "working_directory"
            }:
                raise ValueError("task query action schema mismatch")
            if any(not isinstance(action.get(key), str) for key in action):
                raise ValueError("task query action fields must be strings")
    return tasks


def _inspect_selected_task(
    store: Store,
    record: dict,
    *,
    runner=subprocess.run,
) -> dict:
    task_name = record.get("task_name")
    env = os.environ.copy()
    env["POWERSHELL_UPDATECHECK"] = "Off"
    try:
        with supervisor_lifecycle.selected_host_for_spawn(store) as current:
            if (
                current["selection_revision"] != record["selection_revision"]
                or current["selection_fingerprint"] != record["selection_fingerprint"]
            ):
                raise ValueError("selection changed before task query")
            completed = runner(  # noqa: S603  # nosec B603
                [str(current["path"]), "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-Command", _TASK_QUERY_COMMAND],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                check=False,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if completed.returncode != 0:
            raise ValueError(f"selected-host task query exited {completed.returncode}")
        tasks = _parse_task_query(completed.stdout or "")
    except (OSError, ValueError, subprocess.SubprocessError,
            supervisor_lifecycle.SupervisorLifecycleError) as exc:
        return {"status": "unknown", "detail": str(exc)}
    supervisor_path = store.dir / "supervisor.ps1"

    def targets_checkout(task: dict) -> bool:
        for action in task["actions"]:
            match = _TASK_FILE_ARGUMENT_RE.search(action["arguments"])
            if match is None:
                continue
            file_arg = next((value for value in match.groups() if value is not None), "")
            if psh.normalized_path_key(file_arg) == psh.normalized_path_key(supervisor_path):
                return True
        return False

    targeted = [task for task in tasks if targets_checkout(task)]
    orphans = [
        task for task in targeted
        if not isinstance(task_name, str) or not task_name or task["name"] != task_name
    ]
    if orphans:
        names = ", ".join(repr(task["name"]) for task in orphans)
        return {
            "status": "orphan",
            "detail": f"task binding(s) {names} also target this checkout",
            "tasks": orphans,
        }
    if not isinstance(task_name, str) or not task_name:
        return {"status": "not_configured", "detail": "no task binding is recorded"}
    named = [task for task in tasks if task["name"] == task_name]
    if not named:
        return {"status": "missing", "detail": f"recorded task {task_name!r} is absent"}
    if len(named) != 1:
        return {
            "status": "ambiguous",
            "detail": f"{len(named)} tasks match recorded name {task_name!r}",
            "tasks": named,
        }
    task = named[0]
    actions = task["actions"]
    if len(actions) != 1:
        return {
            "status": "mismatch",
            "detail": f"registered task has {len(actions)} actions; expected exactly one",
            "task": task,
        }
    action = actions[0]
    expected = sup.expected_task_action(store)
    mismatches: list[str] = []
    if psh.normalized_path_key(action["execute"]) != psh.normalized_path_key(str(record["path"])):
        mismatches.append("Execute")
    if action["arguments"] != expected["arguments"]:
        mismatches.append("Arguments")
    if psh.normalized_path_key(action["working_directory"]) != psh.normalized_path_key(
        expected["working_directory"]
    ):
        mismatches.append("WorkingDirectory")
    if task["name"] != task_name:
        mismatches.append("TaskName")
    if mismatches:
        return {
            "status": "mismatch",
            "detail": "registered task fields differ: " + ", ".join(mismatches),
            "task": task,
        }
    return {"status": "ok", "detail": f"{task_name} is {task['state']}", "task": task}


def _check_powershell_artifacts(store: Store) -> Check | None:
    if not any((store.dir / Path(relative)).exists() for relative in sup.ARTIFACT_RELATIVE_PATHS):
        return None
    result = sup.inspect_artifact_bundle(store)
    if result["ok"]:
        marker = next(iter(result["markers"].values()), {})
        return Check(
            name="powershell_artifacts",
            status="ok",
            details=(
                "all four generated artifacts match schema/generation/content "
                f"({marker.get('generator_generation', 'unknown')})"
            ),
            data=result,
        )
    return Check(
        name="powershell_artifacts",
        status="error",
        details="generated PowerShell artifacts are stale or mixed: " + "; ".join(result["errors"]),
        fix=psh.REFRESH_REMEDIATION,
        data=result,
    )


def _check_coordination_stalls(store: Store) -> Check | None:
    """Surface explicit-edge stalls and malformed observational evidence."""
    try:
        from agenttalk import coordination_stall

        snapshot = coordination_stall.build_snapshot(store)
    except Exception as exc:  # noqa: BLE001 - doctor never crashes
        return Check(
            name="coordination_stall",
            status="warn",
            details=f"coordination stall evidence could not be read: {exc}",
            fix="Inspect .agenttalk/state coordination records.",
        )
    items = snapshot.get("items") or []
    diagnostics = snapshot.get("diagnostics") or []
    if not items and not diagnostics:
        return None
    details = [
        str(item.get("reason"))
        for item in items
        if isinstance(item, dict) and item.get("reason")
    ]
    details.extend(
        f"invalid coordination evidence: {row.get('code', 'unknown')}"
        for row in diagnostics
        if isinstance(row, dict)
    )
    actions = [
        str(item.get("action"))
        for item in items
        if isinstance(item, dict) and item.get("action")
    ]
    return Check(
        name="coordination_stall",
        status="warn",
        details="; ".join(details),
        fix=actions[0] if actions else "Inspect .agenttalk/state coordination records.",
        data=snapshot,
    )


def _check_lead_unarmed(store) -> Check | None:
    """Surface a lead-loop identity not armed to consume its team mailbox.

    MANAGED identities (managed_lead_loop = the wrapped controller) MUST be
    continuously armed: NOT armed is an ERROR - the controller is down and team
    messages pile up unhandled. armed = a present managed lease that is NOT stealable
    (the exact complement of the steal predicate), i.e. NOT confirmed-dead AND NOT
    (EXPIRED *and* heartbeat-stale). The failure cases are: no lease, a CONFIRMED-dead
    owner, or a lease that is expired AND heartbeat-stale. An UNKNOWN-liveness owner
    (uncertain probe) is NOT confirmed-dead, so it is treated as probably-alive and is
    ARMED within TTL (WP1: only a definitive dead signal unarms on liveness). NOTE:
    neither expiry NOR a lapsed heartbeat ALONE is an error - an expired-but-
    heartbeating lease and a within-TTL lease whose heartbeat merely lapsed (a long
    healthy turn) are both still ARMED; only the BOTH-stale case is a genuinely down
    controller. The heartbeat window is the agent's resolved threshold (the supervisor
    stuck_after for a wrapped agent via lead_loop_runtime.resolve_timing, else the
    120s store default), so a wrapped controller is judged against its OWN window.
    LEGACY identities (a manual role=lead / operator_facing
    liaison that is NOT managed) get a NON-GATING WARN, and only when they have OPEN
    team work AND are not currently live (no fresh heartbeat or waiter). The legacy
    path is best-effort: a free-form liaison that does not run the heartbeat hook
    writes no heartbeat, so this can over-warn - hence WARN, never ERROR, so it never
    false-blocks a busy liaison. Absent (None) when there is nothing to flag."""
    import time as _time
    from . import lead_loop_runtime as _llr
    from . import threads as _th
    try:
        cfg = store.load_config()
    except Exception:  # noqa: BLE001 - doctor never crashes
        return None
    roster = cfg.get("agents", []) or []
    roles = cfg.get("roles", {}) or {}
    liaison = store.operator_facing()
    now = _time.time()
    # Resolve the lead-loop heartbeat window from supervisor.json (if present) so a
    # WRAPPED managed agent is judged against its supervisor stuck threshold, not the
    # 120s store default - else doctor would false-ERROR a within-window wrapped
    # controller before the supervisor (or a duplicate) would call it stuck (WP1).
    sup_cfg: dict = {}
    try:
        _sp = store.dir / "supervisor.json"
        if _sp.exists():
            _data = json.loads(_sp.read_text(encoding="utf-8-sig"))
            sup_cfg = _data if isinstance(_data, dict) else {}
    except (ValueError, OSError):
        sup_cfg = {}
    errors: list[str] = []
    warns: list[str] = []
    data: dict = {"managed": [], "legacy": []}

    def _open_work(a: str) -> int:
        try:
            closed = {rid for rid, e in store.read_threadstate(a).items()
                      if isinstance(e, dict) and e.get("closed") is True}
            ths = _th.derive_threads(store.valid_messages(), agent=a,
                                     cursor=store.cursor(a) or "",
                                     closed_rids=closed,
                                     retired=set(store.retired_agents()))
        except Exception:  # noqa: BLE001 - best-effort; never crash doctor
            return 0
        return sum(1 for t in ths if getattr(t, "state", None) in _th.ACTIONABLE_STATES)

    for a in roster:
        if store.is_managed_lead_loop(a):
            hsa = _llr.resolve_timing(
                store, a, supervisor_config=sup_cfg or None)["heartbeat_stale_after"]
            st = store.lead_loop_state(a, now=now, heartbeat_stale_after=hsa)
            data["managed"].append({"agent": a, "armed": st["armed"], "reason": st["reason"]})
            if not st["armed"]:
                errors.append(f"{a}: managed lead-loop NOT armed ({st['reason']})")
            continue
        is_lead = isinstance(roles.get(a), str) and roles[a].casefold() == "lead"
        if not (is_lead or a == liaison):
            continue
        if store.agent_active(a, now=now):
            continue  # fresh heartbeat or fresh waiter -> live, do not warn
        open_work = _open_work(a)
        if open_work:
            warns.append(f"{a}: legacy lead/liaison looks un-armed with {open_work} open "
                         f"actionable thread(s) (no fresh heartbeat or waiter)")
            data["legacy"].append({"agent": a, "open_work": open_work})
    if not errors and not warns:
        return None
    return Check(
        name="lead_loop",
        status="error" if errors else "warn",
        details="; ".join(errors + warns),
        fix=("A MANAGED lead-loop must be running its wrapped controller (it owns the "
             "mailbox via a renewable lease) - start/restart it, then check "
             "`agenttalk managed-lead-loop list` + `agenttalk status`. A LEGACY "
             "lead/liaison warning is advisory: run the heartbeat hook so armed-vs-idle "
             "is distinguishable, or migrate it to a managed lead-loop."),
        data=data)


def _check_dead_letter_escalations(store) -> Check | None:
    """LOUD signal for a high-attempt backstop escalation that did NOT route (no
    operator_facing/sole_lead resolved). A known-infra message NEVER auto-dead-letters, so
    without this it could loop under backoff forever with no operator-visible signal
    (codex P2 / lead F2). Absent unless such a record exists."""
    try:
        unrouted = store.list_unrouted_escalations()
    except Exception as e:  # noqa: BLE001 - doctor never crashes
        return Check(name="dead_letter_escalation", status="warn",
                     details=f"could not scan attempt ledgers: {e}")
    if not unrouted:
        return None
    summary = "; ".join(f"{u['agent']}/{u['message_id']} "
                        f"(class={u.get('last_failure_class')}, attempts={u.get('attempts')})"
                        for u in unrouted[:5])
    return Check(
        name="dead_letter_escalation", status="error",
        details=(f"{len(unrouted)} message(s) hit the dead-letter escalation backstop but "
                 f"the operator notice could NOT route (no operator_facing liaison and no "
                 f"sole lead) - a stuck/infra message is retrying with no operator signal. "
                 f"{summary}"),
        fix=("Set a liaison (`agenttalk roster --set-operator-facing <agent>`) or a single "
             "role=lead so the wrapper's dead-letter escalation reaches the operator."),
        data={"unrouted": unrouted})


def _check_attention_dispositions(store) -> Check | None:
    """Surface torn/invalid lines in the attention disposition log (0.56.0). WARN-only and
    additive (absent when the log is clean or missing): a torn line is skipped by the
    fail-safe reader and never hides an active item, but the operator should know the log
    has damage. Never errors/crashes doctor."""
    try:
        from agenttalk import attention as _attn
        _valid, problems = _attn.read_dispositions(store)
    except Exception as e:  # noqa: BLE001 - doctor never crashes
        return Check(name="attention_dispositions", status="warn",
                     details=f"could not read the attention disposition log: {e}")
    if not problems:
        return None
    sample = "; ".join(f"line {p.get('line')}: {p.get('error')}" for p in problems[:3])
    return Check(name="attention_dispositions", status="warn",
                 details=f"{len(problems)} torn/invalid disposition line(s) (skipped, never "
                         f"hide an active item): {sample}",
                 fix="inspect .agenttalk/attention/dispositions.jsonl; the log is append-only "
                     "+ skip-invalid, so a torn tail is safe but worth noting",
                 data={"problems": problems})


def _drop_resolved_dead_letters(store, items: list) -> list:
    """Filter out operator-RESOLVED dead-letters (0.56.0). The central disposition log is
    authoritative. Fail-safe: any read/parse error returns the list UNCHANGED (treat all as
    unresolved) so resolved-awareness can never hide a real dead-letter or crash doctor."""
    try:
        from agenttalk import attention as _attn
        folded = _attn.fold_dispositions(_attn.read_dispositions(store)[0])
        resolved = {
            tuple(iid.split(":", 2)[1:]) for iid, fams in folded.items()
            if iid.startswith(_attn.SOURCE_DEAD_LETTER + ":")
            and fams.get("dead_letter_resolution", {}).get("action")
            == _attn.ACTION_RESOLVE_DEAD_LETTER
        }
    except Exception:  # noqa: BLE001 - resolved-awareness must never crash doctor
        return items
    return [m for m in items
            if (m.get("agent"), m.get("message_id")) not in resolved]


def _check_dead_letter(store) -> Check | None:
    """Surface dead-lettered (poison) messages; absent when there are none. WARN that
    valid messages were dropped (recoverable via `dead-letter list/requeue`), and go to
    a LOUD ERROR when dead-letters exist but NO escalation target resolves
    (operator_facing / sole_lead both unset) - the operator notice could not route, so
    the only signal must not be a silent count."""
    try:
        raw_n = store.dead_lettered_count()
    except Exception as e:  # noqa: BLE001 - doctor never crashes
        return Check(name="dead_letter", status="warn",
                     details=f"could not scan dead-letter sink: {e}")
    if not raw_n:
        return None
    items: list = []
    list_ok = True
    try:
        items = store.list_dead_letters()
    except Exception:  # noqa: BLE001
        list_ok = False
        items = []
    if list_ok:
        # Resolved-aware (0.56.0): an operator-RESOLVED dead-letter is no longer nagged.
        # Central disposition log is authoritative; fail-safe (a read error keeps all items).
        items = _drop_resolved_dead_letters(store, items)
        if not items:
            return None       # nothing unresolved left to nag about
        n = len(items)
    else:
        n = raw_n             # unreadable sink -> LOUD path below with the raw count
    summary = "; ".join(
        f"{m.get('agent')}/{m.get('message_id')} (class={m.get('class')}, "
        f"attempts={m.get('attempts')})" for m in items[:5])
    target = store.operator_facing() or store.sole_lead()
    # Mirror the notifier's reachability (cli._dead_letter_notifier): an agent CANNOT escalate
    # to ITSELF, so a dead-lettering agent that is the only resolvable target gets a notice
    # that never routes - a SILENT disposal. Go LOUD when no target resolves at all OR when
    # any agent in the sink could only "escalate" to itself (lead C5, no-silent-disposal).
    dl_agents = sorted({m.get("agent") for m in items if m.get("agent")})
    self_only = [a for a in dl_agents if target == a]
    # If the sink has dead-letters but we could NOT enumerate them (list raised -> items=[]),
    # we cannot verify any of them routed - fail LOUD rather than infer a benign WARN (a silent
    # disposal we just can't see is still a silent disposal). C5/verify P1.
    unreadable = bool(n) and not items
    data = {"count": n, "messages": items}
    if target is None or self_only or unreadable:
        if target is None:
            why = ("NO escalation target resolves (no operator_facing liaison and no sole "
                   "lead) - the dead-letter notice cannot route.")
        elif self_only:
            why = (f"the only escalation target ({target}) is the dead-lettering agent "
                   f"itself for {', '.join(self_only)} - an agent cannot escalate to itself, "
                   "so the notice does not route.")
        else:
            why = ("the dead-letter sink could not be enumerated to verify the operator "
                   "notices routed (the list read failed) - routing is unverifiable.")
        return Check(
            name="dead_letter", status="error",
            details=(f"{n} dead-lettered poison message(s) but {why} " + summary),
            fix=("Set a DIFFERENT liaison (`agenttalk roster --set-operator-facing "
                 "<other-agent>`) or add a second non-disposing lead, then review "
                 "`agenttalk dead-letter list`."),
            data=data)
    return Check(
        name="dead_letter", status="warn",
        details=(f"{n} dead-lettered poison message(s) (valid messages the model could "
                 f"not process; moved out of the inbox, recoverable). {summary}"),
        fix="Review with `agenttalk dead-letter list`; recover with `dead-letter requeue` "
            "(re-injects a fresh copy but keeps the original here), then `dead-letter resolve "
            "--reason ...` once handled to quiet this warning.",
        data=data)


def _check_knowledge(store) -> Check | None:
    """Surface corrupt/torn lines in the knowledge notes.jsonl (fail-safe reader);
    absent unless the store exists."""
    from agenttalk import knowledge as kn
    if not kn.notes_path(store).exists():
        return None
    try:
        events, problems = kn.read_events(store)
        _views, semantic_problems = kn.resolve_views_with_problems(events)
        problems = [*problems, *semantic_problems]
    except Exception as e:  # noqa: BLE001 - doctor never crashes
        return Check(name="knowledge_notes", status="warn",
                     details=f"could not scan notes.jsonl: {e}")
    if problems:
        return Check(
            name="knowledge_notes", status="warn",
            details=(f"{len(problems)} invalid or non-causal line(s) in notes.jsonl "
                     f"(skipped; valid notes unaffected): "
                     + "; ".join(f"line {p['line']}: {p['error']}" for p in problems[:5])),
            data={"valid": len(events), "problems": problems},
        )
    return Check(name="knowledge_notes", status="ok",
                 details=f"{len(events)} valid knowledge event(s), no ledger problems")


# ---------------------------------------------------------- individual checks

def _check_multi_store(resolved_root: Path, *, cwd: Path | None = None) -> Check:
    """Split-brain detection (#13): name every store from CWD upward.

    The production "--root gotcha" was two ``init``s at different depths:
    both stores valid, neither erroring, two windows silently resolving
    to different roots. One store: quiet OK. Two or more: WARN naming all
    of them in walk order, with the join remediation. WARN, not error — a
    deliberately nested store (``init --force``, e.g. a test sandbox) is
    legitimate; the human decides. Also flags (informationally) a pinned
    root (flag/env) that differs from what the walk would have chosen.
    """
    cwd = (cwd or Path.cwd()).resolve()
    stores = find_stores_upward(cwd)
    data = {
        "cwd": str(cwd),
        "stores": [str(p / ".agenttalk") for p in stores],
        "resolved_root": str(resolved_root),
        "walk_choice": str(stores[0]) if stores else None,
    }
    pinned_note = ""
    if stores and stores[0] != resolved_root:
        # Not a failure: the operator pinned a root the walk wouldn't
        # have chosen. Surface it so "why is my window elsewhere?" has
        # an answer.
        pinned_note = (f" · NOTE: root pinned to {resolved_root} by "
                       f"--root/AGENTTALK_ROOT; the walk from {cwd} would "
                       f"have chosen {stores[0]}")
    if len(stores) >= 2:
        listing = ", ".join(str(p / ".agenttalk") for p in stores)
        return Check(
            name="multi_store",
            status="warn",
            details=(f"{len(stores)} stores on the path from {cwd} upward: "
                     f"{listing} — windows at different depths may resolve "
                     f"to DIFFERENT stores and silently talk past each "
                     f"other{pinned_note}"),
            fix=("make every window agree: pass --root <intended> or set "
                 "AGENTTALK_ROOT; if the nesting was deliberate "
                 "(init --force sandbox), ignore this"),
            data=data,
        )
    if len(stores) == 1:
        return Check(
            name="multi_store",
            status="ok",
            details=f"one store: {stores[0] / '.agenttalk'}{pinned_note}",
            data=data,
        )
    return Check(
        name="multi_store",
        status="ok",
        details=f"no store found from {cwd} upward{pinned_note or ' (rootless cwd)'}",
        data=data,
    )


def _is_wrapped_or_managed_lead(store: Store, agent: str) -> bool:
    if store.is_managed_lead_loop(agent):
        return True
    p = store.dir / "supervisor.json"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(cfg, dict):
        return False
    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        return False
    entry = agents.get(agent)
    return isinstance(entry, dict) and bool(entry.get("wrapped"))


def _interactive_hook_warning(
    *,
    target: str,
    descriptor: str,
    heartbeat_state: str,
    hook_state: str,
) -> Check:
    base = f"{descriptor} {heartbeat_state}; pending escalations may sit unread"
    interactive_fix = (
        f"run `agenttalk supervise --install-activity-hook --interactive-for {target}` "
        f"in the interactive Claude project, or have {target} start listening "
        f"(`agenttalk wait --for {target}`)"
    )
    if hook_state == "neutral":
        return Check(
            name="operator_facing",
            status="warn",
            details=(f"{base}; neutral Claude activity heartbeat hook needs "
                     "AGENTTALK_SELF in this window"),
            fix=(f"set AGENTTALK_SELF={target} before launching this window, "
                 f"or run `agenttalk supervise --install-activity-hook "
                 f"--interactive-for {target}`"),
        )
    if hook_state == "fallback-other":
        return Check(
            name="operator_facing",
            status="warn",
            details=(f"{base}; Claude activity heartbeat hook is bound to the "
                     "wrong identity"),
            fix=(f"run `agenttalk supervise --install-activity-hook "
                 f"--interactive-for {target}` to bind the hook to {target}"),
        )
    if hook_state == "fallback-matching":
        return Check(
            name="operator_facing",
            status="warn",
            details=(f"{base}; fallback hook is installed but no fresh "
                     "heartbeat arrived"),
            fix=("reload or restart the interactive Claude window, then run a "
                 f"tool call; otherwise have {target} start listening "
                 f"(`agenttalk wait --for {target}`)"),
        )
    if hook_state == "unreadable":
        return Check(
            name="operator_facing",
            status="warn",
            details=(f"{base}; .claude/settings.json is unreadable, so doctor "
                     "could not inspect the heartbeat hook"),
            fix=interactive_fix,
        )
    return Check(
        name="operator_facing",
        status="warn",
        details=(f"{base}; no Claude activity heartbeat hook is installed"),
        fix=interactive_fix,
    )


def _check_operator_facing(store: Store) -> Check:
    """Liaison designation health (#18). Advisory routing metadata only —
    diagnostics phrase routing/visibility facts, never enforcement."""
    raw = store.operator_facing_raw()
    resolved = store.operator_facing()
    sole = store.sole_lead()
    if raw is None and sole is None:
        # Legitimate for pairs/single-window teams — INFO. But once
        # escalation traffic exists, workers are asking for an operator
        # channel that has no contracted owner: WARN. One pass over the
        # validated set; cheap at production scale.
        has_escalations = any(
            (m.meta or {}).get("needs_operator")
            for m in store.valid_messages()
        )
        if has_escalations:
            return Check(
                name="operator_facing",
                status="warn",
                details=("not configured, but operator escalations exist in "
                         "the log — they route to whoever was targeted, "
                         "with no liaison contract"),
                fix="run `agenttalk roster set-operator-facing <agent>`",
            )
        return Check(
            name="operator_facing",
            status="ok",
            details="not configured (fine for a pair; teams with one "
                    "human operator should designate a liaison)",
        )
    if resolved is None:
        if raw is None and sole is not None:
            resolved = sole
            descriptor = f"sole lead {resolved}"
        else:
            return Check(
                name="operator_facing",
                status="error",
                details=f"configured liaison {raw!r} is NOT in the roster — "
                        f"escalations will refuse",
                fix=("run `agenttalk roster set-operator-facing <agent>` with a "
                     "rostered name, or `... set-operator-facing --clear`"),
            )
    else:
        descriptor = f"liaison {resolved}"
    if _is_wrapped_or_managed_lead(store, resolved):
        return Check(
            name="operator_facing",
            status="ok",
            details=f"{descriptor}: wrapped/managed lead loop handles liveness",
        )
    if store.agent_active(resolved):
        return Check(
            name="operator_facing",
            status="ok",
            details=f"{descriptor}: active",
        )
    hook_state = sup.classify_claude_activity_hook_state(store.root, resolved)
    hb = store.read_heartbeat(resolved)
    if hb is None:
        # Never listened (or an unreadable/corrupt heartbeat — read_heartbeat
        # collapses both to None). This is the exact scenario this check exists
        # to catch — escalations routed to a liaison nobody is reading — so it
        # must WARN, not fall through to OK (review).
        return _interactive_hook_warning(
            target=resolved,
            descriptor=descriptor,
            heartbeat_state=("is configured but has never listened "
                             "(no readable heartbeat)"),
            hook_state=hook_state,
        )
    age = (datetime.now(timezone.utc) - hb).total_seconds()
    if age > 300:  # same 5-min rule as the heartbeat checks
        return _interactive_hook_warning(
            target=resolved,
            descriptor=descriptor,
            heartbeat_state=f"last seen {int(age)}s ago",
            hook_state=hook_state,
        )
    return Check(
        name="operator_facing",
        status="ok",
        details=f"liaison: {resolved}",
    )


def _check_escalation_target(store: Store) -> Check | None:
    """No-human-facing-target nudge (0.24.0, feedback 3.1).

    Warns a MULTI-agent team that has neither a resolvable liaison nor a sole
    lead: `agenttalk escalate` would have nowhere to route an operator question.
    Returns None — the check is ABSENT — for a solo roster, or when a liaison OR
    a sole lead exists, so healthy and single-window setups stay quiet. Advisory
    only; it can only push the report to `warn`, never `error`.

    Distinct from `_check_operator_facing`, which is about liaison freshness and
    fires only once escalation traffic exists; this one is proactive and fires
    on the structural gap before any escalation is attempted.
    """
    cfg = store.load_config()
    agents = cfg.get("agents", []) or []
    if len(agents) < 2:
        return None  # solo: nothing to escalate between; never warn
    if store.operator_facing() is not None or store.sole_lead() is not None:
        return None  # a liaison OR a lead is a valid escalation target
    return Check(
        name="escalation_target",
        status="warn",
        details=("no human-facing escalation target: this team has neither a "
                 "liaison nor a lead, so `agenttalk escalate` has nowhere to go"),
        fix=("designate one: `agenttalk roster set-operator-facing <agent>` "
             "(liaison) or `agenttalk roster set-role <agent> lead`"),
        data={"agents": len(agents)},
    )


def _check_identity_registry(store: Store) -> Check:
    """Identity registry health (#19 Phase A). Reports active/retired counts and
    flags a dangling rename lineage. Trusted-team metadata only — advisory,
    never an authorization boundary (config.json is no more trustworthy than the
    roster). load_config already fail-closes on an active∩retired overlap or an
    unsafe name, so a corrupt registry shows up as the init check erroring; this
    check assumes a loadable config and surfaces the softer findings."""
    cfg = store.load_config()
    active = cfg.get("agents", []) or []
    retired = store._retired_names(cfg)
    data = {"active": len(active), "retired": len(retired)}
    if not retired:
        return Check(name="identity_registry", status="ok",
                     details=f"{len(active)} active, 0 retired", data=data)
    # Dangling lineage: a renamed_to that points at neither an active identity
    # nor another tombstone. Not an error — the tombstone is still valid and its
    # history stays readable; the named successor is simply absent (e.g. later
    # force-removed). WARN so an operator notices.
    known = set(active) | set(retired)
    dangling = []
    for e in cfg.get("retired") or []:
        rn = e.get("renamed_to") if isinstance(e, dict) else None
        if isinstance(rn, str) and rn and rn not in known:
            dangling.append(f"{e.get('name')}->{rn}")
    if dangling:
        return Check(
            name="identity_registry", status="warn",
            details=(f"{len(active)} active, {len(retired)} retired; dangling "
                     f"rename lineage: {', '.join(dangling)} (successor not in "
                     f"the roster)"),
            fix="informational — the tombstone stays valid; the successor is absent",
            data={**data, "dangling": dangling})
    return Check(
        name="identity_registry", status="ok",
        details=(f"{len(active)} active, {len(retired)} retired "
                 f"(tombstones permanent; history stays valid)"),
        data=data)


def _check_store_hygiene(store: Store) -> Check:
    """Store debris visibility (0.15.0, #17): invalid + quarantined counts.

    Counts come from the SAME store methods every other surface uses
    (list_invalid_messages / quarantined_count) - never a re-implemented
    scan. Quarantine is recoverable, so its count is informational; only
    live invalid files draw a warn, with the inspect-first remediation
    (--dry-run before the move - the install-skills --force lesson).
    """
    invalid = len(store.list_invalid_messages())
    quarantined = store.quarantined_count()
    data = {"invalid": invalid, "quarantined": quarantined}
    if invalid:
        details = f"{invalid} INVALID message file(s) in messages/"
        if quarantined:
            details += f" (+{quarantined} already quarantined)"
        return Check(
            name="store_hygiene",
            status="warn",
            details=details,
            fix=("inspect with `agenttalk prune --invalid --dry-run`, then "
                 "run it without --dry-run to quarantine (recoverable - "
                 "restore by moving the file back into messages/)"),
            data=data,
        )
    if quarantined:
        return Check(
            name="store_hygiene",
            status="ok",
            details=(f"clean; {quarantined} quarantined file(s) held in "
                     f".agenttalk/quarantine/ (recoverable)"),
            data=data,
        )
    return Check(name="store_hygiene", status="ok", details="clean", data=data)


def _check_deadman_config_source(store: Store) -> Check | None:
    """Warn when deadman settings are placed in the ignored supervisor config."""
    supervisor_path = store.dir / "supervisor.json"
    try:
        config = sup.load_supervisor_config(supervisor_path)
    except (OSError, ValueError):
        return None  # the supervisor config health checks own malformed files
    if "deadman" not in config:
        return None
    return Check(
        name="deadman_config_source",
        status="warn",
        details=(
            "supervisor.json contains a deadman block that is ignored; "
            "deadman config is read from config.json"
        ),
        fix=(
            "move the deadman block to .agenttalk/config.json, then remove it "
            "from .agenttalk/supervisor.json"
        ),
        data={
            "ignored_path": str(supervisor_path),
            "active_path": str(store.config_path),
        },
    )


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
    side_flag = " --claude-only" if side == "claude" else " --codex-only"
    if missing:
        return Check(
            name=name,
            status="error",
            details=(
                f"{len(missing)}/{total} missing under {target}"
                f" ({', '.join(missing)})"
            ),
            fix=f"run `agenttalk install-skills{side_flag}`",
            data={"target": str(target), "missing": missing, "differs": differs,
                  "total": total},
        )
    if differs:
        return Check(
            name=name,
            status="warn",
            details=(
                f"{len(differs)}/{total} differ from bundled version under {target}"
                f" ({', '.join(differs)})"
            ),
            # Lead the user to inspect first, then overwrite. The
            # previous fix line jumped straight to --force, which
            # destroys any local edits without warning. --dry-run
            # --force is a no-write preview of what --force would do.
            fix=(
                f"inspect with `agenttalk install-skills{side_flag} --dry-run --force`, "
                f"then overwrite with `agenttalk install-skills{side_flag} --force` "
                f"(local edits will be lost)"
            ),
            data={"target": str(target), "missing": missing, "differs": differs,
                  "total": total},
        )
    return Check(
        name=name,
        status="ok",
        details=f"{total}/{total} in sync at {target}",
        data={"target": str(target), "missing": [], "differs": [],
              "total": total},
    )


def _check_skill_currency() -> Check:
    """Source-content currency of the BUNDLED skill files (distinct from ``_check_skills`` /
    ``_check_devkit``, which check INSTALL freshness). Mechanical lint only: frontmatter
    well-formedness + ``reviewed-against`` ratchet + CLI-token validity vs the live argparse
    surface (``cli.build_parser()``). It proves referenced commands/flags exist and metadata
    is present; it does NOT prove the prose is semantically correct.

    Severity is WARN: stale skill prose is serious but must not make the bus unusable. CI and
    the release gate treat bundled-source regressions as failures via the source-tree test.
    """
    from agenttalk import skill_currency as skc
    try:
        findings = skc.check_bundled_skills(iskl.SKILLS_ROOT, __version__)
    except Exception as e:  # noqa: BLE001 - a lint failure must never crash doctor
        return Check(name="skill_currency", status="warn",
                     details=f"skill-currency lint could not run: {e}",
                     fix="report this; the lint should degrade, not crash")
    if not findings:
        return Check(name="skill_currency", status="ok",
                     details="bundled skills pass currency lint (frontmatter + stamps + CLI tokens)")
    # split by severity: errors are real drift (red the source-tree gate); warnings are
    # advisory (e.g. a reviewed-against version lag). Doctor stays WARN when EITHER exists.
    errors = skc.blocking_findings(findings)
    warnings = skc.warning_findings(findings)
    lead = errors or warnings
    sample = "; ".join(f"{f.file}:{f.line} {f.reason}" for f in lead[:5])
    more = "" if len(lead) <= 5 else f" (+{len(lead) - 5} more)"
    return Check(
        name="skill_currency", status="warn",
        details=f"{len(errors)} blocking + {len(warnings)} advisory skill-currency "
                f"issue(s): {sample}{more}",
        fix="fix blocking drift (stamps/CLI tokens/parity); re-stamp reviewed-against on "
            "review (see docs/skill-devkit-evolution-design.md)",
        data={"findings": [{"file": f.file, "line": f.line, "token": f.token,
                            "reason": f.reason, "level": f.level} for f in findings]},
    )


def _check_devkit() -> Check:
    """Freshness of the dev-discipline pack (devkit) against BOTH Agent-Skills
    dirs (~/.claude/skills + ~/.codex/skills).

    The pack installs by default but is OPT-OUT (`--no-devkit`), so a FULL
    absence is reported OK (the user may have opted out) — just surfaced with
    the install hint rather than silently passing. A PARTIAL or STALE install
    (e.g. an upgrade that didn't re-run install-skills) is a real problem and
    WARNs. Distinct from the bus skills, whose absence is an error.
    """
    pairs = iskl._devkit_pairs(
        iskl.default_claude_skills_dir(), iskl.default_codex_skills_dir(),
    )
    if not pairs:
        return Check(name="devkit_skills", status="ok",
                     details="no devkit pack bundled in this build")
    missing: list[str] = []
    differs: list[str] = []
    for src, dst in pairs:
        if not dst.exists():
            missing.append(str(dst))
            continue
        try:
            if not filecmp.cmp(src, dst, shallow=False):
                differs.append(str(dst))
        except OSError:
            differs.append(str(dst))
    total = len(pairs)
    data = {"total": total, "missing": missing, "differs": differs}
    if len(missing) == total:
        return Check(
            name="devkit_skills", status="ok",
            details=("dev-discipline pack not installed (optional). Add it with "
                     "`agenttalk install-skills --devkit-only` — or this is "
                     "expected if you used --no-devkit."),
            data=data,
        )
    if missing:
        return Check(
            name="devkit_skills", status="warn",
            details=f"{len(missing)}/{total} devkit files missing (incomplete install)",
            fix="run `agenttalk install-skills --devkit-only`",
            data=data,
        )
    if differs:
        return Check(
            name="devkit_skills", status="warn",
            details=(f"{len(differs)}/{total} devkit files differ from the bundled "
                     f"version (stale after upgrade?)"),
            fix=("inspect with `agenttalk install-skills --devkit-only --dry-run --force`, "
                 "then overwrite with `agenttalk install-skills --devkit-only --force` "
                 "(local edits will be lost)"),
            data=data,
        )
    return Check(
        name="devkit_skills", status="ok",
        details=f"{total}/{total} devkit files in sync",
        data=data,
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
    if st.get("duplicate_sections"):
        return Check(
            name="codex_config",
            status="warn",
            details=(f"{st['duplicate_sections']} duplicate [projects] table(s) for "
                     f"{st['project_dir']} — invalid TOML the codex CLI rejects "
                     "(pre-0.75.3 BOM corruption)"),
            fix="run `agenttalk codex-config --enable` to collapse the duplicates",
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
    # in_project_dir is checked BEFORE readability/validity: a key committed
    # inside the project defeats the whole threat model, so surface that
    # specific misconfiguration even if the file is also unreadable/invalid.
    if health.in_project_dir:
        return Check(
            name="hmac", status="error",
            details=f"key file is INSIDE the project at {health.path}",
            fix="move it under the per-user keys dir (defeats the threat model otherwise)",
        )
    if not health.readable:
        return Check(
            name="hmac", status="error",
            details=f"key file at {health.path} is not readable by this process",
        )
    if not health.valid:
        # File exists and is readable but does not parse as a >=16-byte hex
        # key. signing_enforced() is existence-only, so without this branch
        # doctor would report enabled-OK while every signed read is refused
        # (garbage key) or forgeable (the old empty/short-key gap) — review C*.
        return Check(
            name="hmac", status="error",
            details=f"key file at {health.path} is invalid: {health.key_error}",
            fix="regenerate it with `agenttalk hmac-init --force`",
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
    """One check per agent — is anyone actually listening right now?

    #105: a fresh heartbeat alone is the WRAPPER'S OWN self-report - it
    cannot notice its own CLI child dying, so it keeps ticking "ok" after the
    child is gone. When the supervisor has an independently-verified strict
    verdict for this agent (auto_restart-managed agents only), a confirmed
    dead child downgrades this check to ``error`` and an unverifiable one to
    at least ``warn``, regardless of how fresh the heartbeat looks; the
    heartbeat detail is kept, not dropped, as secondary context.
    """
    cfg = store.load_config()
    now = datetime.now(timezone.utc)
    verdicts = sup.strict_child_verdicts(store, now_epoch=now.timestamp())
    out: list[Check] = []
    for a in cfg.get("agents", []):
        hb = store.read_heartbeat(a)
        if hb is None:
            status = "warn"
            details = "no heartbeat — agent has never run `agenttalk wait`"
        else:
            age = (now - hb).total_seconds()
            if age > 300:  # 5 min
                status = "warn"
                details = f"stale (last seen {int(age)}s ago); peer probably not listening"
            else:
                status = "ok"
                details = f"last seen {int(age)}s ago"
        verdict_state = verdicts.get(a, {}).get("state")
        if verdict_state in sup.CLI_CHILD_CONFIRMED_DEAD_STATES:
            status = "error"
            details = (f"supervisor confirms the CLI child is {verdict_state} "
                       f"(heartbeat alone is not proof of life: {details})")
        elif verdict_state in sup.CLI_CHILD_UNVERIFIABLE_STATES:
            if status == "ok":
                status = "warn"
            details = (f"supervisor could not verify the CLI child is alive "
                       f"({verdict_state}); heartbeat: {details}")
        out.append(Check(name=f"heartbeat.{a}", status=status, details=details))
    return out


def _check_active_waiters(store: Store) -> Check | None:
    """Report which agents currently have a LIVE `.waiting` marker, with the
    owning PID (0.18.0, FR-009). Returns None — the check is ABSENT — when no
    agent has a live waiting marker (a malformed/dead/missing marker reads as
    no waiter), so a quiet store adds nothing to the report.

    Advisory only. One window per agent is the assumed model; a marker whose
    PID is alive means a process is actively waiting as that agent right now.
    This is NOT a complete duplicate-detection registry — a single per-agent
    marker can only name the CURRENT owner, not every concurrent window — so
    the wording says "currently waiting", never "all duplicates". `doctor`'s
    exit code is unaffected (this never errors).
    """
    from agenttalk.store import _process_alive  # local: avoid import churn
    cfg = store.load_config()
    live: list[dict] = []
    for a in cfg.get("agents", []) or []:
        marker = store.read_waiting(a)  # None on absent/corrupt — never raises
        if not isinstance(marker, dict):
            continue
        pid = marker.get("pid")
        if isinstance(pid, int) and _process_alive(pid):
            live.append({"agent": a, "pid": pid})
    if not live:
        return None  # additive: no advisory when nobody is actively waiting
    listing = ", ".join(f"{w['agent']} (PID {w['pid']})" for w in live)
    return Check(
        name="active_waiters",
        status="ok",
        details=(f"active waiting marker(s): {listing}. The PID is alive, but "
                 f"PID reuse means this is advisory — a marker owner, not a "
                 f"guaranteed live `agenttalk wait`; it is not a complete "
                 f"duplicate check."),
        data={"live_waiters": live},
    )


_SHIM_EXTENSIONS = frozenset({".cmd", ".bat", ".ps1"})
_AGENTTALK_CMD_PIN_RE = re.compile(
    r'^\s*(?:if\s+not\s+defined\s+AGENTTALK_PYTHON\s+)?'
    r'set\s+"AGENTTALK_PYTHON=([^"]+)"\s*$',
    re.IGNORECASE | re.MULTILINE,
)
_SUPERVISOR_PS1_PIN_RE = re.compile(
    r"^\s*\$AgenttalkPython\s*=\s*'((?:''|[^'])*)'\s*$",
    re.MULTILINE,
)


def _resolve_configured_executable(raw: object, *, label: str) -> tuple[str | None, str, str]:
    """Resolve a configured executable enough for doctor observability.

    Returns (candidate, status, reason). Candidate is retained for display even
    when it is not runnable; callers only probe when status == "ok".
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "warn", f"{label} is missing"
    text = raw.strip()
    if text.startswith("REPLACE"):
        return text, "warn", f"{label} is still a REPLACE placeholder"
    resolved = shutil.which(text) or text
    suffix = Path(resolved).suffix.lower()
    if suffix in _SHIM_EXTENSIONS:
        return resolved, "warn", f"{label} resolves to shim {resolved}"
    p = Path(resolved)
    if not p.exists() or not p.is_file():
        return resolved, "warn", f"{label} was not found"
    return str(p), "ok", "resolved"


def _wrapped_base_tail(agent_cfg: dict) -> tuple[object | None, str | None]:
    launch = agent_cfg.get("launch") if isinstance(agent_cfg.get("launch"), dict) else {}
    args = launch.get("windows_args")
    if not isinstance(args, list):
        return None, "wrapped launch.windows_args is missing or not a list"
    if "--" not in args:
        return None, "wrapped launch is missing -- before the real Codex executable"
    tail = args[args.index("--") + 1:]
    if not tail:
        return None, "wrapped launch has no real CLI tail after --"
    return tail[0], None


def _read_agenttalk_cmd_pin(store: Store) -> str | None:
    p = store.dir / "bin" / "agenttalk.cmd"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _AGENTTALK_CMD_PIN_RE.search(text)
    return m.group(1).strip() if m else None


def _read_supervisor_ps1_pin(store: Store) -> str | None:
    p = store.dir / "supervisor.ps1"
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    m = _SUPERVISOR_PS1_PIN_RE.search(text)
    return m.group(1).replace("''", "'").strip() if m else None


def _check_supervisor_script_guard(store: Store) -> Check | None:
    p = store.dir / "supervisor.ps1"
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        return Check(
            name="supervisor_script",
            status="warn",
            details=f"could not read supervisor.ps1 to verify singleton guard: {e}",
            fix="regenerate the supervisor script with `agenttalk supervise --init --force`",
        )
    has_claim = "--claim-instance" in text
    has_release = "--release-instance" in text
    if has_claim and has_release:
        return None
    missing = []
    if not has_claim:
        missing.append("--claim-instance")
    if not has_release:
        missing.append("--release-instance")
    return Check(
        name="supervisor_script",
        status="warn",
        details=(
            "supervisor.ps1 is missing the singleton guard "
            f"({', '.join(missing)}); multiple old supervisors can run concurrently"
        ),
        fix="regenerate it with `agenttalk supervise --init --force`",
        data={"path": str(p), "missing": missing},
    )


def _resolve_agenttalk_pin(store: Store | None, *, wrapped: bool,
                           wrapper_python: str | None) -> tuple[str, str, str | None]:
    if wrapped and wrapper_python:
        return wrapper_python, "launch.windows_file", None
    if store is not None:
        cmd_pin = _read_agenttalk_cmd_pin(store)
        if cmd_pin:
            return cmd_pin, "agenttalk.cmd", None
        ps_pin = _read_supervisor_ps1_pin(store)
        if ps_pin:
            return ps_pin, "supervisor.ps1", "agenttalk pin fell back to supervisor.ps1 parse"
    return sys.executable, "sys.executable", "agenttalk pin fell back to doctor sys.executable"


def _codex_home_info(store: Store | None, agent: str, agent_cfg: dict) -> tuple[str | None, str, str | None]:
    if store is None:
        return None, "not_checked", None
    isolation = bool(agent_cfg.get("codex_home_isolation", True))
    if not isolation:
        return None, "not_expected", None
    p = store.dir / "codex-home" / agent
    if p.is_dir():
        return str(p), "existing", None
    return str(p), "missing_expected", f"expected seeded CODEX_HOME is missing for {agent}"


def _source_checkout_src(root: Path) -> Path | None:
    src = root / "src"
    return src if (src / "agenttalk" / "__init__.py").exists() else None


_CRITICAL_LAUNCH_ENV_KEYS = frozenset({"AGENTTALK_ROOT", "AGENTTALK_PY", "PYTHONPATH", "CODEX_HOME"})


def _set_env_case_insensitive(env: dict[str, str], key: str, value: str) -> None:
    for existing in list(env):
        if existing.casefold() == key.casefold() and existing != key:
            env.pop(existing, None)
    env[key] = value


def _probe_env(store: Store, launch: dict, agent_cfg: dict) -> tuple[dict[str, str], list[str]]:
    env = os.environ.copy()
    managed = {
        "AGENTTALK_ROOT": str(store.root),
        "AGENTTALK_PY": str(launch["agenttalk_py"]),
    }
    src = _source_checkout_src(store.root)
    if src is not None:
        old = env.get("PYTHONPATH")
        managed["PYTHONPATH"] = str(src) + (os.pathsep + old if old else "")
    if launch.get("codex_home_status") == "existing" and launch.get("codex_home_path"):
        managed["CODEX_HOME"] = str(launch["codex_home_path"])
    for key, value in managed.items():
        _set_env_case_insensitive(env, key, value)

    warnings: list[str] = []
    managed_by_casefold = {key.casefold(): key for key in managed}
    overrides = agent_cfg.get("env") if isinstance(agent_cfg.get("env"), dict) else {}
    for key, raw_value in overrides.items():
        if not isinstance(key, str):
            continue
        value = "" if raw_value is None else str(raw_value)
        managed_key = managed_by_casefold.get(key.casefold())
        target_key = managed_key or key
        _set_env_case_insensitive(env, target_key, value)
        if managed_key in _CRITICAL_LAUNCH_ENV_KEYS and managed[managed_key] != value:
            warnings.append(f"agent.env overrides managed {managed_key}; doctor probes the effective override")
    return env, warnings


def resolve_supervised_codex_launch(agent: str, agent_cfg: dict,
                                    store: Store | None = None) -> dict:
    """Structured doctor view of the launch facts for one supervised Codex agent."""
    launch_cfg = agent_cfg.get("launch") if isinstance(agent_cfg.get("launch"), dict) else {}
    wrapped = bool(agent_cfg.get("wrapped"))
    warnings: list[str] = []

    wrapper_python = None
    wrapper_python_status = "not_wrapped"
    wrapper_python_reason = ""
    if wrapped:
        wrapper_python, wrapper_python_status, wrapper_python_reason = _resolve_configured_executable(
            launch_cfg.get("windows_file"), label="wrapped launch.windows_file",
        )
        if wrapper_python_status != "ok":
            warnings.append(wrapper_python_reason)
        raw_base, tail_warning = _wrapped_base_tail(agent_cfg)
        if tail_warning:
            base_cli, base_cli_status, base_cli_reason = None, "warn", tail_warning
        else:
            base_cli, base_cli_status, base_cli_reason = _resolve_configured_executable(
                raw_base, label="wrapped Codex tail",
            )
    else:
        base_cli, base_cli_status, base_cli_reason = _resolve_configured_executable(
            launch_cfg.get("windows_file"), label="launch.windows_file",
        )
    if base_cli_status != "ok":
        warnings.append(base_cli_reason)

    codex_home_path, codex_home_status, codex_home_warning = _codex_home_info(store, agent, agent_cfg)
    if codex_home_warning:
        warnings.append(codex_home_warning)
    agenttalk_py, pin_provenance, pin_warning = _resolve_agenttalk_pin(
        store, wrapped=wrapped, wrapper_python=wrapper_python if wrapper_python_status == "ok" else None,
    )
    env_mirror = "full"
    if codex_home_status == "missing_expected":
        env_mirror = "partial"
    if pin_provenance == "sys.executable":
        env_mirror = "doctor_fallback"
    if pin_warning:
        warnings.append(pin_warning)

    return {
        "agent": agent,
        "wrapped": wrapped,
        "wrapper_python": wrapper_python,
        "wrapper_python_status": wrapper_python_status,
        "wrapper_python_reason": wrapper_python_reason,
        "base_cli": base_cli,
        "base_cli_status": base_cli_status,
        "base_cli_reason": base_cli_reason,
        "codex_home_path": codex_home_path,
        "codex_home_status": codex_home_status,
        "agenttalk_py": agenttalk_py,
        "agenttalk_py_provenance": pin_provenance,
        "env_mirror": env_mirror,
        "warnings": warnings,
    }


def _resolve_supervised_codex_exe(agent_cfg: dict) -> str | None:
    """Compatibility adapter for the old private str/None contract."""
    rec = resolve_supervised_codex_launch("agent", agent_cfg, None)
    return rec["base_cli"] if rec["base_cli_status"] == "ok" else None


def _default_codex_runner(exe: str, args: list[str], timeout: float,
                          cwd: str | Path | None = None, env: dict[str, str] | None = None):
    """Run exe args best-effort; return (returncode, combined_output) or
    (None, reason) on any failure. Timeout-bounded so doctor never hangs; never
    shell (operator-configured exe path)."""
    try:
        # operator-configured codex exe path; argv is a list, never shell.
        p = subprocess.run(  # noqa: S603  # nosec B603
            [exe, *args], capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd is not None else None, env=env,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, type(e).__name__


def _check_agenttalk_runtime(project_root: Path) -> str | None:
    from agenttalk.wrapper import run as wrapper_run
    return wrapper_run.preflight_agenttalk_runtime(workspace_root=project_root)


def _call_probe(runner, exe: str, args: list[str], timeout: float,
                cwd: Path, env: dict[str, str]) -> tuple[object | None, str]:
    try:
        res = runner(exe, args, timeout, cwd, env)
    except Exception as e:  # noqa: BLE001 - doctor probe must degrade, not crash
        return None, type(e).__name__
    if not isinstance(res, tuple) or len(res) != 2:
        return None, "invalid runner result"
    rc, out = res
    return rc, str(out or "")


def _probe_version(runner, exe: str | None, args: list[str], *,
                   cwd: Path, env: dict[str, str], label: str) -> dict:
    if not exe:
        return {"status": "warn", "version": None, "reason": f"{label} executable is unresolved"}
    rc, out = _call_probe(runner, exe, args, 5.0, cwd, env)
    if rc != 0:
        return {"status": "warn", "version": None,
                "reason": f"{label} probe failed ({rc if rc is not None else out})"}
    version = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    if not version:
        return {"status": "warn", "version": None, "reason": f"{label} probe returned UNVERSIONED"}
    return {"status": "ok", "version": version, "reason": ""}


def _check_supervised_codex(store: Store, *, runner=None, runtime_checker=None) -> Check | None:
    """Surface supervised Codex launch/probe observability.

    Absent when there is no configured supervised Codex; OK when resolution, runtime
    probes, and env mirror are complete; WARN for probe/env drift. Returns ERROR when a
    WRAPPED supervised Codex fails the ``agenttalk`` runtime preflight (launch-blocking,
    via ``runtime_checker``) — the child cannot run agenttalk in the workspace so the
    supervised loop would wedge. Never mutates launch state.
    """
    runner = runner or _default_codex_runner
    sup_path = store.dir / "supervisor.json"
    if not sup_path.exists():
        return None
    try:
        cfg = json.loads(sup_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None  # the supervisor's own --init/validate owns a malformed config
    if not isinstance(cfg, dict):
        return None
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    codex_agents: list[tuple[str, dict]] = [
        (name, a) for name, a in agents.items()
        if isinstance(a, dict) and a.get("cli") == "codex"
    ]
    if not codex_agents:
        return None

    entries: list[dict] = []
    any_warn = False
    for name, agent_cfg in codex_agents:
        rec = resolve_supervised_codex_launch(name, agent_cfg, store)
        cwd = Path(agent_cfg.get("cwd") if isinstance(agent_cfg.get("cwd"), str) else store.root)
        env, env_warnings = _probe_env(store, rec, agent_cfg)
        if env_warnings and rec["env_mirror"] == "full":
            rec = {**rec, "env_mirror": "partial"}
        warnings = list(rec["warnings"])
        warnings.extend(env_warnings)
        codex_probe = (
            _probe_version(runner, rec["base_cli"], ["--version"], cwd=cwd, env=env, label="codex")
            if rec["base_cli_status"] == "ok"
            else {"status": "warn", "version": None, "reason": rec["base_cli_reason"]}
        )
        if codex_probe["status"] != "ok":
            warnings.append(str(codex_probe["reason"]))
        wrapper_probe = {"status": "skipped", "version": None, "reason": ""}
        if rec["wrapped"]:
            wrapper_probe = (
                _probe_version(runner, rec["wrapper_python"], ["-m", "agenttalk", "--version"],
                               cwd=cwd, env=env, label="wrapper python")
                if rec["wrapper_python_status"] == "ok"
                else {"status": "warn", "version": None, "reason": rec["wrapper_python_reason"]}
            )
            if wrapper_probe["status"] != "ok":
                warnings.append(str(wrapper_probe["reason"]))
        agenttalk_probe = _probe_version(
            runner, env.get("AGENTTALK_PY") or rec["agenttalk_py"], ["-m", "agenttalk", "--version"],
            cwd=cwd, env=env, label="AGENTTALK_PY",
        )
        if agenttalk_probe["status"] != "ok":
            warnings.append(str(agenttalk_probe["reason"]))
        if rec["env_mirror"] != "full":
            warnings.append(f"env_mirror={rec['env_mirror']}")
        entry = {
            **rec,
            "version": codex_probe["version"],
            "codex_probe_status": codex_probe["status"],
            "codex_probe_reason": codex_probe["reason"],
            "wrapper_probe_status": wrapper_probe["status"],
            "wrapper_version": wrapper_probe["version"],
            "wrapper_probe_reason": wrapper_probe["reason"],
            "agenttalk_probe_status": agenttalk_probe["status"],
            "agenttalk_version": agenttalk_probe["version"],
            "agenttalk_probe_reason": agenttalk_probe["reason"],
            "sandbox_probe_status": "skipped",
            "warnings": warnings,
        }
        if warnings:
            any_warn = True
        entries.append(entry)

    lines = []
    for e in entries:
        version = e["version"] or "UNVERSIONED"
        target = e["base_cli"] or "UNRESOLVED"
        line = (f"{e['agent']} -> {target} [{version}] env_mirror={e['env_mirror']} "
                f"agenttalk_py={e['agenttalk_py_provenance']}")
        if e["warnings"]:
            line += " WARN: " + "; ".join(dict.fromkeys(e["warnings"]))
        lines.append(line)
    details = "; ".join(lines)
    # Runtime preflight (dev-3 sign-off): a WRAPPED supervised Codex can only launch if
    # `agenttalk` actually runs in the workspace. A blocker here is launch-blocking, so
    # surface it as an ERROR (doctor holds the agent) rather than an advisory WARN.
    if runtime_checker is not None and any(e.get("wrapped") for e in entries):
        blocker = runtime_checker(store.root)
        if blocker:
            return Check(
                name="supervised_codex",
                status="error",
                details=("agenttalk-runtime-preflight-FAILED: " + str(blocker)
                         + (f" | {details}" if details else "")),
                fix=str(blocker),
                data={"codex": entries, "agenttalk_runtime": blocker},
            )
    if any_warn:
        return Check(
            name="supervised_codex",
            status="warn",
            details=details,
            fix=("fill the native codex.exe path in supervisor.json (or the wrapped tail after --), "
                 "avoid .cmd/.bat/.ps1 shims, ensure AGENTTALK_PY can run -m agenttalk, seed the "
                 "per-agent CODEX_HOME when isolation is enabled, and use explicit Codex --add-dir "
                 "<python-dir> / writable_roots opt-in if workspace-write denies the pinned Python."),
            data={"codex": entries},
        )
    return Check(
        name="supervised_codex",
        status="ok",
        details=details,
        data={"codex": entries},
    )


def _check_config_blocked_holds(store: Store) -> Check | None:
    try:
        cfg = store.load_config()
    except Exception:  # noqa: BLE001 - init check owns corrupt config
        return None
    holds: list[dict] = []
    for agent in cfg.get("agents", []) or []:
        try:
            hold = store.read_config_blocked_hold(str(agent))
        except Exception:  # noqa: BLE001 - doctor never crashes on state files
            hold = None
        if hold is not None:
            holds.append(hold)
    if not holds:
        return None
    details = "; ".join(
        f"{h['agent']}: {h.get('summary') or 'config_blocked launch/runtime hold'}"
        for h in holds
    )
    return Check(
        name="config_blocked_holds",
        status="warn",
        details=details,
        fix=("repair supervisor launch config, fill the native codex.exe path or set AGENTTALK_CODEX "
             "where supported, explicitly opt in to the pinned Python directory with Codex --add-dir "
             "<python-dir> / writable_roots if workspace-write denies it, then run "
             "`agenttalk request-restart --for <agent>`."),
        data={"holds": holds},
    )
