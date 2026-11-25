# Requirements & Reference Implementation: `agenttalk` Interactive Setup Wizard & Declarative Team Config

> **Maintainers' note (2026-11-24).** This is a feature proposal that the operator shared. Its code (section 5) and test plan (section 6) illustrate the idea; they are not the implementation and were not reviewed as one. Two independent reviews, one by a Claude seat and one by a Codex seat, decided what is taken and when. The guided questions, presets, preview and "first 5 minutes" guide go into the first-team set-up, [#866](https://github.com/zoolok17/agenttalk/issues/866). The team file, the guidance rules and the round warnings are later stages in [#915](https://github.com/zoolok17/agenttalk/issues/915), each with its own challenge first. The settings this proposal calls enforced (autonomy levels, round limits, internet and path rules) are guidance for the agents unless a later stage builds a real check. `agenttalk init` keeps its current meaning.

**Status:** Draft for Dev Team Review  
**Target Version:** `agenttalk` > 0.100.0  
**Scope:** Interactive CLI setup wizard (`agenttalk wizard` / `agenttalk init`), declarative configuration schema (`.agenttalk/team.toml`), idempotent reconciliation (`agenttalk team apply`), autonomy & circuit-breaker policies, and operational guardrails.

---

## 1. Problem Statement & Goals

Setting up a new `agenttalk` team currently requires manual orchestration across multiple primitives: configuring the roster (`lead`, `liaison`, `trust-class`), hand-authoring `.agenttalk/domains.json` so `lane assign` succeeds, installing Claude/Codex skills, and establishing team norms for reviews, challenges, and escalations.

For a new user who only has the reference documentation, this initial setup is a high-friction barrier.

### Core Goals
1. **Zero-to-Working Team in Minutes:** Provide an interactive CLI wizard (`agenttalk wizard` / `agenttalk init`) that guides a new user through plain-language questions and generates a valid, working team setup.
2. **Declarative & Version-Controllable Source of Truth:** Persist the wizard's output into a human-readable, commented config file (`.agenttalk/team.toml`) that can be committed to git, edited by hand later, and re-applied idempotently.
3. **Explicit Autonomy & Deadlock Controls:** Allow teams to configure both human-operator autonomy and intra-team autonomy (AI lead control vs. seat autonomy and peer-to-peer discussion scope), backed by a **hard circuit-breaker round cap** independent of autonomy level.
4. **Operational Sandboxing:** Define clear, enforceable permissions around internet/package access, filesystem/worktree/domain boundaries, and destructive or high-risk actions.
5. **Respect `agenttalk` Hard Invariants:** Never bypass built-in safety constraints (e.g., single lead per store, paid-gateway `external-worker` seats cannot be `lead` or `liaison`, merge gate checks, and validated `.agenttalk/domains.json`).

---

## 2. Functional Requirements

### 2.1 CLI Entry Points & Modes
* **`agenttalk wizard` (alias: `agenttalk init`):**
  * Launches an interactive terminal prompt flow using standard library / existing CLI dependencies (no heavy TUI framework required).
  * Supports `--preset <name>` to pre-seed answers (`solo-light`, `standard-feature`, `high-assurance`).
  * Supports `--non-interactive` (with `--preset` or `--config <path>`) for deterministic execution in scripts or agent-assisted setup.
  * Supports `--dry-run` to preview the generated `.agenttalk/team.toml` and state mutations without writing files or modifying the store.
  * **Existing State Handling:** If `.agenttalk/` already exists, inspect current roster, domains, and config; offer options to:
    1. Adopt/import existing roster and domains into `.agenttalk/team.toml`,
    2. Merge/update safely, or
    3. Re-initialize from a chosen preset (preserving message history/knowledge store unless explicitly reset).
* **`agenttalk team apply [--config .agenttalk/team.toml] [--dry-run]`:**
  * Reads `.agenttalk/team.toml`, validates schema and `agenttalk` invariants, shows a diff of planned changes, and idempotently reconciles:
    * Roster entries, roles, and `operator-facing` (liaison) assignment.
    * `.agenttalk/domains.json` (validated via `agenttalk domain validate`).
    * Installed skills (`agenttalk install-skills`, respecting `--no-devkit` if configured).
    * Rendered policy/briefing context injected into wrapped turns so seats and the lead honor configured autonomy, circuit breakers, and permissions.
* **`agenttalk team validate [--config .agenttalk/team.toml]`:**
  * Offline validation of schema types, allowed enum values, and invariant checks (e.g., rejecting `external-worker` assigned as `lead` or `liaison`, verifying at least one domain exists if `enforce_domain_boundaries = true`).

---

### 2.2 Wizard Step-by-Step User Flow

The wizard should ask **6 focused, plain-language steps**, explaining *why* each choice matters inline:

1. **Step 1: Starting Preset (Team Shape)**
   * `solo-light`: Lead/Liaison + 1 Developer + 1 Reviewer. Lighter ceremony for fast iteration.
   * `standard-feature` *(Default)*: Lead/Liaison + Developer + Reviewer + Challenger (for major changes). Balanced gates and worktree isolation.
   * `high-assurance`: Multi-vendor cross-review/challenge pairing, mandatory challenges on major work, strict path/network guardrails, and assurance close readiness.
2. **Step 2: Helm & Supervision Mode**
   * Combined Lead + Liaison seat *(default)* vs. separate Lead and Liaison seats.
   * Lead execution mode: `interactive` (human drives lead window) vs. `managed-loop` (`agenttalk wrap --loop --lead-loop`).
3. **Step 3: Seats & Available Assistants**
   * Detect installed CLIs (`claude`, `codex`) on `PATH` and suggest seat assignments.
   * Map seats to roles (`lead`, `developer`, `reviewer`, `architect`, `tester`, `challenger`) and vendors (`anthropic`, `openai`, etc.) to support cross-vendor review/challenge rules.
   * Optional paid-gateway (`external-worker`) seat configuration with automatic enforcement that it is never assigned `lead` or `liaison`.
4. **Step 4: Autonomy & Deadlock Circuit Breakers**
   * **Operator Autonomy (`autonomy.operator`):**
     * `ask-often`: Escalate to human before major work, scope shifts, spending, sensitive paths, or trade-offs.
     * `balanced` *(Default)*: Execute scoped tasks and standard review/fix loops autonomously; escalate on money, security, irreversible actions, major scope changes, or blockers.
     * `high-autonomy`: Execute end-to-end across challenge, build, review, and gates; report at milestones or hard blockers.
   * **Lead vs. Seat Autonomy (`autonomy.lead_control`):**
     * `lead-tight`: Lead assigns and approves every step and handoff closely.
     * `lead-guided` *(Default)*: Lead sets goals, lanes, and acceptance criteria; seats execute within their lane and report completion or friction.
     * `seat-autonomous`: Seats self-direct implementation steps within their assigned lane/work item.
   * **Peer-to-Peer Discussion (`autonomy.peer_discussion` & `autonomy.peer_scope`):**
     * `restricted`: Communication flows through the lead via typed tasks/replies.
     * `scoped-consult` *(Default)*: Peers may hold bounded 1:1 consults/proposals on **implementation details only**; **scope and trade-off decisions** must go to the AI lead.
     * `open-peer`: Peers may collaborate directly across multiple seats on implementation details and bring synthesized recommendations or unresolved trade-offs to the AI lead.
   * **Hard Circuit-Breaker Caps (`limits.*` — Independent of Autonomy):**
     * `max_peer_rounds` *(Default: 3)*: Maximum back-and-forth rounds between peers on a topic before they must either decide (if within implementation scope) or hand off to the AI lead.
     * `max_lead_rounds` *(Default: 3)*: Maximum clarification/negotiation rounds between the AI lead and a seat on an issue before the AI lead must make a binding call or escalate to the human operator ("we got stuck, now what").
     * Applies uniformly regardless of whether `autonomy.operator` is `ask-often` or `high-autonomy`.
5. **Step 5: Operational Guardrails & Permissions**
   * **Internet & External Resources:** `none` (offline only) | `restricted` *(Default: read docs/references only; ask before downloading packages or calling external APIs)* | `allow-approved`.
   * **Filesystem & Workspace Boundaries:**
     * `worktree_only = true` *(Default)*: Builders never edit the main checkout directly.
     * `enforce_domain_boundaries = true` *(Default)*: Seed initial code domains (auto-detected from top-level repo folders like `src/`, `docs/`, `tests/`) into `.agenttalk/domains.json`.
     * `forbidden_paths`: Default protection for `.env*`, `secrets/`, and direct edits to `.agenttalk/` internal state files.
   * **High-Impact Action Approvals:** Require explicit approval/escalation for `dependencies`, `deletions`, `auth-or-secrets`, `spending`, and `destructive-git`.
6. **Step 6: Skills, Preview & Verification**
   * Choose whether to install bus skills (`install_bus = true`) and devkit skills (`install_devkit = true`).
   * Display a clean summary of the generated `.agenttalk/team.toml` and state changes, ask for confirmation, apply, run validation checks (`domain validate`, `roster`), and print a **"First 5 Minutes"** quick-start guide.

---

## 3. Declarative Configuration Schema (`.agenttalk/team.toml`)

```toml
# .agenttalk/team.toml — Generated by `agenttalk wizard`
# Edit this file to customize your team, then run `agenttalk team apply`.

[meta]
schema_version = 1
preset = "standard-feature" # solo-light | standard-feature | high-assurance

[helm]
liaison_same_as_lead = true
lead_mode = "interactive"   # interactive | managed-loop
target_branch = "main"

[autonomy]
# How often the team checks with the human operator
operator = "balanced"             # ask-often | balanced | high-autonomy

# How tightly the AI lead controls individual seats
lead_control = "lead-guided"      # lead-tight | lead-guided | seat-autonomous

# How peers collaborate directly vs. routing through the AI lead
peer_discussion = "scoped-consult" # restricted | scoped-consult | open-peer
peer_scope = "implementation-only" # implementation-only | scoped-tradeoffs

[limits]
# Hard circuit breakers enforced regardless of autonomy settings
max_peer_rounds = 3                # max back-and-forth rounds between peers on a topic
max_lead_rounds = 3                # max negotiation rounds between AI lead and a seat
on_cap_reached = "lead-then-human" # lead-then-human | escalate-to-human

[permissions]
# Network & external dependencies
internet_access = "restricted"     # none | restricted | allow-approved
allow_package_installs = false     # true | false (escalate if needed)
allow_external_calls = false       # true | false

# Filesystem, worktree & domain guardrails
worktree_only = true               # true | false (never modify main checkout)
enforce_domain_boundaries = true   # true | false (enforced via .agenttalk/domains.json)
allowed_extra_paths = []           # optional extra writable paths outside worktree/scratch
forbidden_paths = [
  ".env",
  ".env.*",
  "secrets/",
  ".agenttalk/",
]

# Git & high-impact actions
allow_branch_push = true
allow_destructive_git = false
require_approval_for = [
  "dependencies",
  "deletions",
  "auth-or-secrets",
  "spending",
]

[skills]
install_bus = true
install_devkit = true

[[seats]]
name = "lead"
role = "lead"
cli = "claude"
vendor = "anthropic"
trust_class = "internal"

[[seats]]
name = "builder"
role = "developer"
cli = "codex"
vendor = "openai"
trust_class = "internal"

[[seats]]
name = "reviewer"
role = "reviewer"
cli = "claude"
vendor = "anthropic"
trust_class = "internal"

[[seats]]
name = "challenger"
role = "challenger"
cli = "codex"
vendor = "openai"
trust_class = "internal"

[domains.core]
paths = ["src/"]
owners = ["builder"]

[domains.docs]
paths = ["docs/", "README.md"]
owners = ["builder"]
```

---

## 4. Integration with `agenttalk` Runtime & Wrapper

To ensure `.agenttalk/team.toml` is enforced rather than purely decorative:

1. **Wrapper Prompt Policy Block (`src/agenttalk/wrapper/prompt.py`):**
   * When `team.toml` is present, render a compact, deterministic **Team Policy Block** into wrapped seat prompts containing:
     * Active autonomy mode (`operator`, `lead_control`, `peer_discussion`, `peer_scope`).
     * Hard circuit-breaker instruction: *"You have at most `max_peer_rounds` rounds with a peer and `max_lead_rounds` rounds with the lead on any unresolved issue. If unresolved at the cap, stop debating immediately and escalate with options and a recommendation."*
     * Active permissions (`internet_access`, `allow_package_installs`, `worktree_only`, `forbidden_paths`, `require_approval_for`).
2. **Thread Round Tracking (`src/agenttalk/threads.py`):**
   * Track reply depth per thread/topic against `limits.max_peer_rounds` (for `consult`/`proposal`/peer `message` threads) and `limits.max_lead_rounds` (for seat-to-lead threads).
   * Emit a clear warning header in `sync`/`threads` and wrapper turn metadata when a thread reaches `cap - 1` and `cap`, prompting immediate decision or `agenttalk escalate`.
3. **Invariant Validation (`src/agenttalk/team_config.py`):**
   * Enforce that no seat with `trust_class = "external-worker"` has `role = "lead"` or is designated as liaison.
   * Ensure `.agenttalk/domains.json` is generated/updated from `[domains.*]` and passes existing `domain validate` logic before `apply` succeeds.

---

## 5. Reference Implementation Blueprint

Below is a clean, self-contained Python reference module (`src/agenttalk/wizard.py`) illustrating the data structures, invariant validation, preset generation, interactive CLI flow, TOML serialization, and idempotent `apply` reconciliation.

```python
"""Reference implementation for `agenttalk wizard` and `agenttalk team apply`."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


VALID_PRESETS = ("solo-light", "standard-feature", "high-assurance")
VALID_OPERATOR_AUTONOMY = ("ask-often", "balanced", "high-autonomy")
VALID_LEAD_CONTROL = ("lead-tight", "lead-guided", "seat-autonomous")
VALID_PEER_DISCUSSION = ("restricted", "scoped-consult", "open-peer")
VALID_PEER_SCOPE = ("implementation-only", "scoped-tradeoffs")
VALID_INTERNET_ACCESS = ("none", "restricted", "allow-approved")


@dataclass
class SeatConfig:
    name: str
    role: str
    cli: str
    vendor: str
    trust_class: str = "internal"
    model: str | None = None
    backend_profile: str | None = None


@dataclass
class AutonomyConfig:
    operator: str = "balanced"
    lead_control: str = "lead-guided"
    peer_discussion: str = "scoped-consult"
    peer_scope: str = "implementation-only"


@dataclass
class LimitsConfig:
    max_peer_rounds: int = 3
    max_lead_rounds: int = 3
    on_cap_reached: str = "lead-then-human"


@dataclass
class PermissionsConfig:
    internet_access: str = "restricted"
    allow_package_installs: bool = False
    allow_external_calls: bool = False
    worktree_only: bool = True
    enforce_domain_boundaries: bool = True
    allowed_extra_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(
        default_factory=lambda: [".env", ".env.*", "secrets/", ".agenttalk/"]
    )
    allow_branch_push: bool = True
    allow_destructive_git: bool = False
    require_approval_for: list[str] = field(
        default_factory=lambda: [
            "dependencies",
            "deletions",
            "auth-or-secrets",
            "spending",
        ]
    )


@dataclass
class TeamConfig:
    preset: str = "standard-feature"
    liaison_same_as_lead: bool = True
    liaison_seat: str = "lead"
    lead_mode: str = "interactive"
    target_branch: str = "main"
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    install_bus_skills: bool = True
    install_devkit_skills: bool = True
    seats: list[SeatConfig] = field(default_factory=list)
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Validate schema constraints and agenttalk hard invariants."""
        errors: list[str] = []

        if self.preset not in VALID_PRESETS:
            errors.append(f"Invalid preset: {self.preset}")
        if self.autonomy.operator not in VALID_OPERATOR_AUTONOMY:
            errors.append(f"Invalid autonomy.operator: {self.autonomy.operator}")
        if self.autonomy.lead_control not in VALID_LEAD_CONTROL:
            errors.append(f"Invalid autonomy.lead_control: {self.autonomy.lead_control}")
        if self.autonomy.peer_discussion not in VALID_PEER_DISCUSSION:
            errors.append(
                f"Invalid autonomy.peer_discussion: {self.autonomy.peer_discussion}"
            )
        if self.autonomy.peer_scope not in VALID_PEER_SCOPE:
            errors.append(f"Invalid autonomy.peer_scope: {self.autonomy.peer_scope}")
        if self.permissions.internet_access not in VALID_INTERNET_ACCESS:
            errors.append(
                f"Invalid permissions.internet_access: {self.permissions.internet_access}"
            )

        if self.limits.max_peer_rounds < 1 or self.limits.max_lead_rounds < 1:
            errors.append("Circuit-breaker round caps must be >= 1.")

        leads = [s for s in self.seats if s.role == "lead"]
        if len(leads) != 1:
            errors.append(f"Exactly one lead seat is required (found {len(leads)}).")

        seen_names: set[str] = set()
        for seat in self.seats:
            if seat.name in seen_names:
                errors.append(f"Duplicate seat name: {seat.name}")
            seen_names.add(seat.name)

            if seat.trust_class == "external-worker":
                if seat.role == "lead":
                    errors.append(
                        f"Seat '{seat.name}' is an external-worker and cannot be lead."
                    )
                if seat.name == self.liaison_seat:
                    errors.append(
                        f"Seat '{seat.name}' is an external-worker and cannot be liaison."
                    )

        if self.liaison_seat not in seen_names:
            errors.append(f"Liaison seat '{self.liaison_seat}' not found in seats.")

        if self.permissions.enforce_domain_boundaries and not self.domains:
            errors.append(
                "At least one domain is required when enforce_domain_boundaries = true."
            )

        return errors


def detect_available_clis() -> dict[str, bool]:
    """Check which agent CLIs are available on PATH."""
    return {
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
    }


def detect_default_domains(repo_root: Path, owner: str = "builder") -> dict[str, dict[str, Any]]:
    """Infer sensible initial domains from common repository directories."""
    domains: dict[str, dict[str, Any]] = {}
    if (repo_root / "src").is_dir():
        domains["core"] = {"paths": ["src/"], "owners": [owner]}
    elif (repo_root / "lib").is_dir():
        domains["core"] = {"paths": ["lib/"], "owners": [owner]}

    if (repo_root / "tests").is_dir():
        domains["tests"] = {"paths": ["tests/"], "owners": [owner]}

    doc_paths = [p for p in ("docs/", "README.md") if (repo_root / p.rstrip("/")).exists()]
    if doc_paths:
        domains["docs"] = {"paths": doc_paths, "owners": [owner]}

    if not domains:
        domains["project"] = {"paths": ["."], "owners": [owner]}

    return domains


def build_preset(preset: str, repo_root: Path) -> TeamConfig:
    """Create a baseline TeamConfig from a named preset."""
    clis = detect_available_clis()
    primary_cli = "claude" if clis["claude"] else ("codex" if clis["codex"] else "claude")
    secondary_cli = "codex" if (primary_cli == "claude" and clis["codex"]) else primary_cli

    primary_vendor = "anthropic" if primary_cli == "claude" else "openai"
    secondary_vendor = "openai" if secondary_cli == "codex" else "anthropic"

    if preset == "solo-light":
        seats = [
            SeatConfig("lead", "lead", primary_cli, primary_vendor),
            SeatConfig("builder", "developer", primary_cli, primary_vendor),
            SeatConfig("reviewer", "reviewer", secondary_cli, secondary_vendor),
        ]
        autonomy = AutonomyConfig(
            operator="balanced",
            lead_control="lead-guided",
            peer_discussion="scoped-consult",
            peer_scope="implementation-only",
        )
        limits = LimitsConfig(max_peer_rounds=2, max_lead_rounds=3)
        perms = PermissionsConfig(internet_access="restricted")
    elif preset == "high-assurance":
        seats = [
            SeatConfig("lead", "lead", primary_cli, primary_vendor),
            SeatConfig("architect", "architect", primary_cli, primary_vendor),
            SeatConfig("builder", "developer", secondary_cli, secondary_vendor),
            SeatConfig("reviewer", "reviewer", primary_cli, primary_vendor),
            SeatConfig("challenger", "challenger", secondary_cli, secondary_vendor),
        ]
        autonomy = AutonomyConfig(
            operator="ask-often",
            lead_control="lead-tight",
            peer_discussion="restricted",
            peer_scope="implementation-only",
        )
        limits = LimitsConfig(max_peer_rounds=2, max_lead_rounds=2)
        perms = PermissionsConfig(
            internet_access="none",
            allow_package_installs=False,
            allow_external_calls=False,
            worktree_only=True,
            enforce_domain_boundaries=True,
        )
    else:  # standard-feature
        seats = [
            SeatConfig("lead", "lead", primary_cli, primary_vendor),
            SeatConfig("builder", "developer", secondary_cli, secondary_vendor),
            SeatConfig("reviewer", "reviewer", primary_cli, primary_vendor),
            SeatConfig("challenger", "challenger", secondary_cli, secondary_vendor),
        ]
        autonomy = AutonomyConfig()
        limits = LimitsConfig()
        perms = PermissionsConfig()

    return TeamConfig(
        preset=preset,
        autonomy=autonomy,
        limits=limits,
        permissions=perms,
        seats=seats,
        domains=detect_default_domains(repo_root, owner="builder"),
    )


def render_toml(cfg: TeamConfig) -> str:
    """Render a commented, human-editable .agenttalk/team.toml file."""
    lines: list[str] = [
        "# .agenttalk/team.toml — Generated by `agenttalk wizard`",
        "# Edit this file anytime and run `agenttalk team apply` to update your team.",
        "",
        "[meta]",
        "schema_version = 1",
        f'preset = "{cfg.preset}"',
        "",
        "[helm]",
        f"liaison_same_as_lead = {str(cfg.liaison_same_as_lead).lower()}",
        f'liaison_seat = "{cfg.liaison_seat}"',
        f'lead_mode = "{cfg.lead_mode}"',
        f'target_branch = "{cfg.target_branch}"',
        "",
        "[autonomy]",
        "# operator: ask-often | balanced | high-autonomy",
        f'operator = "{cfg.autonomy.operator}"',
        "# lead_control: lead-tight | lead-guided | seat-autonomous",
        f'lead_control = "{cfg.autonomy.lead_control}"',
        "# peer_discussion: restricted | scoped-consult | open-peer",
        f'peer_discussion = "{cfg.autonomy.peer_discussion}"',
        "# peer_scope: implementation-only | scoped-tradeoffs",
        f'peer_scope = "{cfg.autonomy.peer_scope}"',
        "",
        "[limits]",
        "# Hard circuit breakers enforced regardless of autonomy level",
        f"max_peer_rounds = {cfg.limits.max_peer_rounds}",
        f"max_lead_rounds = {cfg.limits.max_lead_rounds}",
        f'on_cap_reached = "{cfg.limits.on_cap_reached}"',
        "",
        "[permissions]",
        f'internet_access = "{cfg.permissions.internet_access}"',
        f"allow_package_installs = {str(cfg.permissions.allow_package_installs).lower()}",
        f"allow_external_calls = {str(cfg.permissions.allow_external_calls).lower()}",
        f"worktree_only = {str(cfg.permissions.worktree_only).lower()}",
        f"enforce_domain_boundaries = {str(cfg.permissions.enforce_domain_boundaries).lower()}",
        f"allowed_extra_paths = {json.dumps(cfg.permissions.allowed_extra_paths)}",
        f"forbidden_paths = {json.dumps(cfg.permissions.forbidden_paths)}",
        f"allow_branch_push = {str(cfg.permissions.allow_branch_push).lower()}",
        f"allow_destructive_git = {str(cfg.permissions.allow_destructive_git).lower()}",
        f"require_approval_for = {json.dumps(cfg.permissions.require_approval_for)}",
        "",
        "[skills]",
        f"install_bus = {str(cfg.install_bus_skills).lower()}",
        f"install_devkit = {str(cfg.install_devkit_skills).lower()}",
        "",
    ]

    for seat in cfg.seats:
        lines.extend(
            [
                "[[seats]]",
                f'name = "{seat.name}"',
                f'role = "{seat.role}"',
                f'cli = "{seat.cli}"',
                f'vendor = "{seat.vendor}"',
                f'trust_class = "{seat.trust_class}"',
            ]
        )
        if seat.model:
            lines.append(f'model = "{seat.model}"')
        if seat.backend_profile:
            lines.append(f'backend_profile = "{seat.backend_profile}"')
        lines.append("")

    for domain_name, spec in cfg.domains.items():
        lines.extend(
            [
                f"[domains.{domain_name}]",
                f"paths = {json.dumps(spec['paths'])}",
                f"owners = {json.dumps(spec['owners'])}",
                "",
            ]
        )

    return "\n".join(lines)


def render_seat_policy_prompt(cfg: TeamConfig) -> str:
    """Render concise operational rules injected into wrapped seat turns."""
    return (
        "## Team Autonomy, Deadlock & Guardrail Policy\n"
        f"- **Human Oversight (`{cfg.autonomy.operator}`):** Escalate to `{cfg.liaison_seat}` "
        f"via `agenttalk escalate` for {', '.join(cfg.permissions.require_approval_for)}, "
        "irreversible actions, or unresolved blockers.\n"
        f"- **Lead Control (`{cfg.autonomy.lead_control}`):** Follow your assigned role and lane.\n"
        f"- **Peer Discussion (`{cfg.autonomy.peer_discussion}` / `{cfg.autonomy.peer_scope}`):** "
        "Peers may discuss implementation details directly within policy, but scope changes and "
        "architectural trade-offs must be routed to the AI lead.\n"
        f"- **Hard Circuit Breaker (Mandatory):** Max `{cfg.limits.max_peer_rounds}` peer-to-peer "
        f"rounds and `{cfg.limits.max_lead_rounds}` lead-to-seat rounds per issue. When the cap "
        "is reached without resolution, stop debating immediately and make a call or escalate to "
        "the human operator ('we got stuck, now what') with options and a recommendation.\n"
        f"- **Permissions:** internet=`{cfg.permissions.internet_access}`, "
        f"package_installs=`{cfg.permissions.allow_package_installs}`, "
        f"worktree_only=`{cfg.permissions.worktree_only}`, "
        f"forbidden_paths={json.dumps(cfg.permissions.forbidden_paths)}.\n"
    )


def apply_team_config(
    cfg: TeamConfig,
    repo_root: Path,
    dry_run: bool = False,
) -> list[str]:
    """Materialize TeamConfig into `.agenttalk/` files and CLI state."""
    errors = cfg.validate()
    if errors:
        raise ValueError("Invalid team configuration:\n- " + "\n- ".join(errors))

    agenttalk_dir = repo_root / ".agenttalk"
    team_toml_path = agenttalk_dir / "team.toml"
    domains_json_path = agenttalk_dir / "domains.json"
    planned_actions: list[str] = []

    toml_content = render_toml(cfg)
    domains_payload = {"domains": cfg.domains}

    planned_actions.append(f"Write {team_toml_path}")
    planned_actions.append(f"Write {domains_json_path}")

    for seat in cfg.seats:
        planned_actions.append(
            f"Ensure roster seat '{seat.name}' (role={seat.role}, trust={seat.trust_class})"
        )
    planned_actions.append(f"Set operator-facing liaison to '{cfg.liaison_seat}'")

    if cfg.install_bus_skills:
        skill_cmd = ["agenttalk", "install-skills"]
        if not cfg.install_devkit_skills:
            skill_cmd.append("--no-devkit")
        planned_actions.append(f"Run {' '.join(skill_cmd)}")

    if dry_run:
        return planned_actions

    agenttalk_dir.mkdir(parents=True, exist_ok=True)
    team_toml_path.write_text(toml_content, encoding="utf-8")
    domains_json_path.write_text(
        json.dumps(domains_payload, indent=2) + "\n", encoding="utf-8"
    )

    # Reconcile roster and validate domains using existing agenttalk CLI primitives
    for seat in cfg.seats:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "agenttalk",
                "roster",
                "add",
                seat.name,
                "--trust-class",
                seat.trust_class,
            ],
            cwd=repo_root,
            check=False,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "agenttalk",
                "roster",
                "set-role",
                seat.name,
                seat.role,
            ],
            cwd=repo_root,
            check=True,
        )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "agenttalk",
            "roster",
            "set-operator-facing",
            cfg.liaison_seat,
        ],
        cwd=repo_root,
        check=True,
    )

    subprocess.run(
        [sys.executable, "-m", "agenttalk", "domain", "validate"],
        cwd=repo_root,
        check=True,
    )

    if cfg.install_bus_skills:
        cmd = [sys.executable, "-m", "agenttalk", "install-skills"]
        if not cfg.install_devkit_skills:
            cmd.append("--no-devkit")
        subprocess.run(cmd, cwd=repo_root, check=True)

    return planned_actions


def _prompt_choice(label: str, options: list[tuple[str, str]], default: str) -> str:
    """Minimal interactive CLI selector."""
    print(f"\n{label}")
    for idx, (key, desc) in enumerate(options, start=1):
        marker = " (default)" if key == default else ""
        print(f"  {idx}. {key}{marker} — {desc}")
    raw = input(f"Choose [1-{len(options)}] or enter name [{default}]: ").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1][0]
    for key, _ in options:
        if raw == key:
            return key
    print(f"Unrecognized choice '{raw}', using default '{default}'.")
    return default


def run_interactive_wizard(repo_root: Path, dry_run: bool = False) -> TeamConfig:
    """Interactive CLI wizard entry point."""
    print("=== agenttalk Team & Workflow Setup Wizard ===")

    preset = _prompt_choice(
        "1. Choose a starting team shape:",
        [
            ("solo-light", "Lead/Liaison + Developer + Reviewer (fast, lighter gates)"),
            ("standard-feature", "Balanced team with Challenger & worktree isolation"),
            ("high-assurance", "Strict multi-vendor reviews, tight oversight & guardrails"),
        ],
        default="standard-feature",
    )
    cfg = build_preset(preset, repo_root)

    cfg.autonomy.operator = _prompt_choice(
        "2. How autonomous should the team be with you (the human operator)?",
        [
            ("ask-often", "Check in frequently before major work, scope shifts, or trade-offs"),
            ("balanced", "Work autonomously on scoped tasks; escalate on risk, money, or blockers"),
            ("high-autonomy", "Push end-to-end across gates; only escalate on hard blockers/risk"),
        ],
        default=cfg.autonomy.operator,
    )

    cfg.autonomy.lead_control = _prompt_choice(
        "3. How much control should the AI Lead keep over individual agents?",
        [
            ("lead-tight", "AI Lead directs every step and approves handoffs closely"),
            ("lead-guided", "AI Lead sets lanes and goals; seats execute within their lane"),
            ("seat-autonomous", "Seats self-direct implementation steps inside assigned work"),
        ],
        default=cfg.autonomy.lead_control,
    )

    cfg.autonomy.peer_discussion = _prompt_choice(
        "4. How much can teammates discuss directly before asking the AI Lead?",
        [
            ("restricted", "Route coordination through the AI Lead"),
            ("scoped-consult", "Peers settle implementation details; trade-offs go to AI Lead"),
            ("open-peer", "Open peer collaboration on details; escalate trade-offs/deadlocks"),
        ],
        default=cfg.autonomy.peer_discussion,
    )

    cfg.permissions.internet_access = _prompt_choice(
        "5. What internet and package download access should the team have?",
        [
            ("none", "Offline / local repo only unless explicitly approved"),
            ("restricted", "Read docs/references; ask before installing packages or calling APIs"),
            ("allow-approved", "Allow standard tooling/package fetches within repo rules"),
        ],
        default=cfg.permissions.internet_access,
    )

    actions = apply_team_config(cfg, repo_root=repo_root, dry_run=dry_run)
    print("\n=== Planned / Applied Setup ===")
    for action in actions:
        print(f"  - {action}")

    return cfg
```

---

## 6. Acceptance Criteria & Test Plan for Dev Team Review

1. **Unit Tests (`tests/test_wizard.py`):**
   * Verify `build_preset` generates valid `TeamConfig` instances for all three presets (`solo-light`, `standard-feature`, `high-assurance`).
   * Verify `validate()` rejects:
     * `external-worker` seats assigned as `lead` or `liaison`.
     * `max_peer_rounds < 1` or `max_lead_rounds < 1`.
     * Empty `domains` when `enforce_domain_boundaries = true`.
   * Verify round-trip serialization (`render_toml` -> `tomllib.loads`) preserves all fields cleanly.
2. **Integration Tests (`tests/test_team_apply.py`):**
   * Against a throwaway store (`agenttalk scratch store`), verify `apply_team_config` creates `.agenttalk/team.toml` and `.agenttalk/domains.json`, passes `agenttalk domain validate`, sets the `lead` and `operator-facing` liaison in `agenttalk roster`, and is idempotent across repeated runs.
3. **Wrapper Prompt & Circuit-Breaker Tests:**
   * Verify wrapped turns include `render_seat_policy_prompt` when `.agenttalk/team.toml` exists.
   * Verify thread depth tracking triggers the circuit-breaker warning/escalation prompt once `max_peer_rounds` or `max_lead_rounds` is reached, even when `autonomy.operator = "high-autonomy"`.