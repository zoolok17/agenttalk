"""Health check command: did the user wire everything up correctly?

`agenttalk doctor` runs a series of small checks and reports
each as ``ok`` / ``warn`` / ``error``, with a remediation hint when
something is off. Designed so a fresh user can self-diagnose
"why isn't the bus working?" without reading the code.
"""

from __future__ import annotations

import filecmp
import json
# subprocess is used ONLY to run the operator-configured codex exe `--version` /
# `sandbox --help` as a best-effort, timeout-bounded diagnostic probe; never shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import __version__
from agenttalk import codex_config as cxc
from agenttalk import install_skills as iskl
from agenttalk import signing as _signing
from agenttalk.store import Store, find_root, find_stores_upward


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
    report = Report(
        agenttalk_version=__version__,
        python_version=py,
        project_root=str(root),
    )

    store = Store(root)
    init_check = _check_init(store)
    report.checks.append(init_check)
    # Multi-store detection runs UNCONDITIONALLY (not gated on an
    # initialized store): the split-brain layout (#13) is most dangerous
    # exactly when the user is confused about which store they're on —
    # including when the resolved root has no store at all.
    report.checks.append(_check_multi_store(root))
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
        report.checks.extend(_check_skills())
        report.checks.append(_check_devkit())
        report.checks.append(_check_codex_config(root))
        report.checks.append(_check_hmac(store, root))
        report.checks.extend(_check_heartbeats(store))
        waiters = _check_active_waiters(store)
        if waiters is not None:  # additive: absent unless a live waiter exists
            report.checks.append(waiters)
        codex_vis = _check_supervised_codex(store)
        if codex_vis is not None:  # additive: absent unless a supervised codex agent
            report.checks.append(codex_vis)
        kn_check = _check_knowledge(store)
        if kn_check is not None:  # additive: absent unless a knowledge store exists
            report.checks.append(kn_check)
        dl_check = _check_dead_letter(store)
        if dl_check is not None:  # additive: absent unless dead-letters exist
            report.checks.append(dl_check)
        esc_check = _check_dead_letter_escalations(store)
        if esc_check is not None:  # additive: absent unless an unrouted backstop exists
            report.checks.append(esc_check)
        lead_check = _check_lead_unarmed(store)
        if lead_check is not None:  # additive: absent unless a lead-loop concern exists
            report.checks.append(lead_check)
    return report


def _check_lead_unarmed(store) -> Check | None:
    """Surface a lead-loop identity not armed to consume its team mailbox.

    MANAGED identities (managed_lead_loop = the wrapped controller) MUST be
    continuously armed: NOT armed is an ERROR - the controller is down and team
    messages pile up unhandled. armed = a present lease whose owner is ALIVE and
    which is NOT stealable (the exact complement of the steal predicate), i.e. the
    failure cases are: no lease, a dead owner, or a lease that is EXPIRED *and*
    heartbeat-stale. NOTE: neither dimension ALONE is an error - an expired-but-
    heartbeating lease and a within-TTL lease whose heartbeat merely lapsed (a long
    healthy turn) are both still ARMED; only the BOTH-stale case (expired AND
    heartbeat-stale) is a genuinely down controller (lead P2 - the prior hb-only
    rule false-ERRORed at the 120s heartbeat window while the lease TTL is 900s).
    LEGACY identities (a manual role=lead / operator_facing
    liaison that is NOT managed) get a NON-GATING WARN, and only when they have OPEN
    team work AND are not currently live (no fresh heartbeat or waiter). The legacy
    path is best-effort: a free-form liaison that does not run the heartbeat hook
    writes no heartbeat, so this can over-warn - hence WARN, never ERROR, so it never
    false-blocks a busy liaison. Absent (None) when there is nothing to flag."""
    import time as _time
    from . import threads as _th
    try:
        cfg = store.load_config()
    except Exception:  # noqa: BLE001 - doctor never crashes
        return None
    roster = cfg.get("agents", []) or []
    roles = cfg.get("roles", {}) or {}
    liaison = store.operator_facing()
    now = _time.time()
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
            st = store.lead_loop_state(a, now=now)
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


def _check_dead_letter(store) -> Check | None:
    """Surface dead-lettered (poison) messages; absent when there are none. WARN that
    valid messages were dropped (recoverable via `dead-letter list/requeue`), and go to
    a LOUD ERROR when dead-letters exist but NO escalation target resolves
    (operator_facing / sole_lead both unset) - the operator notice could not route, so
    the only signal must not be a silent count."""
    try:
        n = store.dead_lettered_count()
    except Exception as e:  # noqa: BLE001 - doctor never crashes
        return Check(name="dead_letter", status="warn",
                     details=f"could not scan dead-letter sink: {e}")
    if not n:
        return None
    items = []
    try:
        items = store.list_dead_letters()
    except Exception:  # noqa: BLE001
        items = []
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
        fix="Review with `agenttalk dead-letter list`; recover with `dead-letter requeue`.",
        data=data)


def _check_knowledge(store) -> Check | None:
    """Surface corrupt/torn lines in the knowledge notes.jsonl (fail-safe reader);
    absent unless the store exists."""
    from agenttalk import knowledge as kn
    if not kn.notes_path(store).exists():
        return None
    try:
        events, problems = kn.read_events(store)
    except Exception as e:  # noqa: BLE001 - doctor never crashes
        return Check(name="knowledge_notes", status="warn",
                     details=f"could not scan notes.jsonl: {e}")
    if problems:
        return Check(
            name="knowledge_notes", status="warn",
            details=(f"{len(problems)} corrupt/torn line(s) in notes.jsonl "
                     f"(skipped; valid notes unaffected): "
                     + "; ".join(f"line {p['line']}: {p['error']}" for p in problems[:5])),
            data={"valid": len(events), "problems": problems},
        )
    return Check(name="knowledge_notes", status="ok",
                 details=f"{len(events)} valid knowledge event(s), no corrupt lines")


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


def _check_operator_facing(store: Store) -> Check:
    """Liaison designation health (#18). Advisory routing metadata only —
    diagnostics phrase routing/visibility facts, never enforcement."""
    raw = store.operator_facing_raw()
    resolved = store.operator_facing()
    if raw is None:
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
        return Check(
            name="operator_facing",
            status="error",
            details=f"configured liaison {raw!r} is NOT in the roster — "
                    f"escalations will refuse",
            fix=("run `agenttalk roster set-operator-facing <agent>` with a "
                 "rostered name, or `... set-operator-facing --clear`"),
        )
    hb = store.read_heartbeat(resolved)
    if hb is None:
        # Never listened (or an unreadable/corrupt heartbeat — read_heartbeat
        # collapses both to None). This is the exact scenario this check exists
        # to catch — escalations routed to a liaison nobody is reading — so it
        # must WARN, not fall through to OK (review).
        return Check(
            name="operator_facing",
            status="warn",
            details=(f"liaison {resolved} is configured but has never listened "
                     f"(no readable heartbeat) — pending escalations may sit "
                     f"unread"),
            fix=f"have {resolved} start listening (`agenttalk wait --for {resolved}`)",
        )
    age = (datetime.now(timezone.utc) - hb).total_seconds()
    if age > 300:  # same 5-min rule as the heartbeat checks
        return Check(
            name="operator_facing",
            status="warn",
            details=(f"liaison {resolved} last seen {int(age)}s ago — "
                     f"pending escalations may sit unread"),
            fix=f"have {resolved} rejoin (`agenttalk sync --for {resolved}`)",
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


def _resolve_supervised_codex_exe(agent_cfg: dict) -> str | None:
    """The codex executable a supervised codex agent will actually run: the base
    argv tail after ``--`` for a wrapped agent (windows_file is python there), else
    ``launch.windows_file``. None when unset / still a REPLACE placeholder."""
    launch = agent_cfg.get("launch") if isinstance(agent_cfg.get("launch"), dict) else {}
    if agent_cfg.get("wrapped"):
        args = launch.get("windows_args") or []
        if isinstance(args, list) and "--" in args:
            tail = args[args.index("--") + 1:]
            exe = tail[0] if tail else None
        else:
            exe = None
    else:
        exe = launch.get("windows_file")
    if not isinstance(exe, str) or not exe or exe.startswith("REPLACE"):
        return None
    return exe


def _default_codex_runner(exe: str, args: list[str], timeout: float):
    """Run ``exe args`` best-effort; return (returncode, combined_output) or
    (None, reason) on any failure. Timeout-bounded so `doctor` never hangs; never
    shell (operator-configured exe path)."""
    try:
        # operator-configured codex exe path; argv is a list, never shell.
        p = subprocess.run(  # noqa: S603  # nosec B603
            [exe, *args], capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, type(e).__name__


def _check_supervised_codex(store: Store, *, runner=None) -> Check | None:
    """0.31.1: surface the RESOLVED codex exe + its ``codex --version`` for each
    supervised codex agent, with a best-effort, NON-blocking ``codex sandbox
    --help`` probe whose failure prints a hint (an old/alpha build - e.g. the
    MS-Store codex - vs the npm stable codex agenttalk expects). A diagnostic only:
    it can WARN but never errors, and never hangs (each probe is timeout-bounded).
    Returns None when there is no supervisor.json or no codex agent configured."""
    runner = runner or _default_codex_runner
    sup_path = store.dir / "supervisor.json"
    if not sup_path.exists():
        return None
    try:
        cfg = json.loads(sup_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None  # the supervisor's own --init/validate owns a malformed config
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    exes: dict[str, list[str]] = {}   # resolved exe -> agent names using it
    for name, a in agents.items():
        if isinstance(a, dict) and a.get("cli") == "codex":
            exe = _resolve_supervised_codex_exe(a)
            if exe:
                exes.setdefault(exe, []).append(name)
    if not exes:
        return None
    entries: list[dict] = []
    any_warn = False
    for exe, names in exes.items():
        rc, out = runner(exe, ["--version"], 5.0)
        version = out.strip().splitlines()[0].strip() if (rc == 0 and out) else None
        sbx_rc, _ = runner(exe, ["sandbox", "--help"], 5.0)
        sandbox_ok = sbx_rc == 0
        if version is None or not sandbox_ok:
            any_warn = True
        entries.append({"exe": exe, "agents": sorted(names),
                        "version": version, "sandbox_probe_ok": sandbox_ok})
    lines = []
    for e in entries:
        v = e["version"] or "UNVERSIONED (codex --version did not run)"
        lines.append(f"{', '.join(e['agents'])} -> {e['exe']} [{v}]"
                     + ("" if e["sandbox_probe_ok"] else " · sandbox probe FAILED"))
    if any_warn:
        return Check(
            name="supervised_codex",
            status="warn",
            details="; ".join(lines),
            fix=("a sandbox probe / version failure may mean a wrong or old/alpha "
                 "codex (e.g. the MS-Store build) - agenttalk expects the npm "
                 "stable codex; check the launch.windows_file / wrap base path"),
            data={"codex": entries},
        )
    return Check(
        name="supervised_codex",
        status="ok",
        details="; ".join(lines),
        data={"codex": entries},
    )
