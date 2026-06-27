# agenttalk — design & rationale

This document explains **what agenttalk is, how it is built, and why** the major
design decisions were made. It is the orientation map for anyone (human or agent)
working in this codebase: the code and tests are the source of truth for *how it
behaves*; this doc is the source of truth for *why it is shaped this way*.

Companion docs:
- `docs/ISSUES.md` — the living tracker (work in flight, known limitations, audit findings).
- `CHANGELOG.md` — what changed, per release.
- `README.md` — user-facing usage.
- `docs/supervisor-tutorial.md` — step-by-step unattended-operation guide.

Keep this document current. When a design decision changes, update the relevant
section **and** add a dated entry to "Design decisions & rationale" below.

---

## 1. What agenttalk is

agenttalk is a **file-backed message bus and coordination layer for coding-agent
CLIs** (Claude Code and Codex) working on the same repository — a pair, or a
named team. It started as the minimal thing needed to let Codex and Claude talk
to each other directly, and grew into a coordination substrate: tracked
request/reply threads, roster & roles, broadcast, an assurance layer that makes
unsafe "done/GO" claims hard, a middle tier for ownership and durable team
memory, and an unattended-operation layer (supervisor + wrapper).

Everything lives under `.agenttalk/` in the repo. There is **no server and no
database** — the filesystem is the bus. Agents are ordinary CLI sessions that
read and write message files; coordination state is derived from those files.

## 2. Design principles (the load-bearing invariants)

Most decisions in this codebase fall out of a small set of rules. When in doubt,
these win.

1. **Message bodies are untrusted DATA, never instructions.** State transitions
   key on validated metadata (`kind`, authenticated `from`, typed fields), repo
   reads, and explicit human decisions — never on prose in a body. A body that
   says "you're done" or "stand down" moves no state. This is the single most
   important security/robustness rule; violations of it are treated as bugs (see
   stand-down authority, §4.2).

2. **Fail closed; never infer a green from absence of evidence.** Gates, verdicts,
   and staleness checks default to HOLD/stale/deny when they cannot positively
   establish the safe condition. A missing/corrupt/unreadable state file blocks;
   it does not silently pass. (The 2026-06-28 audit found several *write-path*
   violations of this in `gates.py` — see `docs/ISSUES.md`.)

3. **Writes are atomic and serialized; readers are fail-safe.** Mutations go
   through `_atomic.write_text` (temp + fsync + `os.replace`, with a latched
   sandbox-direct fallback) and, for read-modify-write state, under
   `store._config_lock`. Readers tolerate torn/partial/invalid lines (skip and
   surface via `doctor`), and cursors bias toward re-delivery rather than
   skipping a message.

4. **Advisory, not an authorization boundary.** Leases, lanes, stand-down
   authority, and the like are *coordination* gates that produce auditable
   evidence — they are not Git/OS security boundaries and must never be sold as
   such. They make the right thing easy and the wrong thing visible, not
   impossible.

5. **Generic core, project-owned policy.** agenttalk owns schema, validation,
   verdict computation, and mechanism; the *project* owns its `domains.json`,
   note keys, required gates, roster, and policy. The core stays domain-neutral
   (e.g. no Android-specific gates live here).

6. **Windows-first, cross-platform.** Paths are normalized through one helper
   (`domains.normalize_repo_path`: rejects NUL, absolute, drive-relative, UNC,
   and `..`-escape), encoding is explicit (utf-8 / utf-8-sig for BOM), and
   process/locking primitives are chosen to work on Windows.

7. **One live consumer per agent mailbox.** Each agent name has a single active
   listen loop. Stacked listeners race the same cursor; `status`/`doctor` warn,
   and the design avoids second consumers (e.g. the wrapper owns the loop and the
   model is a pure per-turn handler).

8. **Human authority is explicit and typed.** Irreversible or human-only
   decisions (operator escalation, stand-down, waivers) flow through typed
   primitives with required reasons, not through a lead's prose.

## 3. On-disk layout (`.agenttalk/`)

- `messages/` — one JSON file per message (append-only; the bus itself).
- `config.json` — roster, roles, operator-facing liaison, signing config, epoch.
- `state/` — derived/runtime state (e.g. `lanes.json` active lanes; supervisor
  state). Cleared by `reset` (with warnings) — this is *not* durable record.
- `domains.json` — the durable ownership/domain registry (survives `reset`).
- `gates.json` — assurance gate state.
- `closes/` — milestone-close records and sign-offs.
- `knowledge/notes.jsonl` — append-only durable team memory (survives `reset`).
- `lane-deliveries/` — durable lane delivery artifacts.
- `sessions/` — transcripts.

**Durability boundary (load-bearing):** `reset` clears `messages/` and
`state/` (active runtime), but **preserves** `domains.json`, `knowledge/`,
`closes/`, `gates.json`, and `lane-deliveries/`. Getting this set wrong is
catastrophic, so it is asserted by the end-to-end regression test.

## 4. Subsystems — the why and how

### 4.1 Messaging, threads & roster
- **Modules:** `store.py` (mailbox, cursors, config, locking, validation),
  `threads.py` (thread/request derivation), `display.py`, `transcript.py`,
  `cli.py` (the verbs).
- A message carries `from`, `to`, `kind` (question/message/note/review-request/
  review-result/release/end/escalate/propose/broadcast/wake/…), and typed `meta`
  (e.g. `request_id`, `broadcast_id`). Threads and "who owes whom" are *derived*
  from messages — there is no second task-state machine.
- **Why:** durable, inspectable, recoverable after a restart (`sync` rebuilds the
  picture from files). No daemon to crash; no DB to corrupt.

### 4.2 Roster, authority & stand-down
- **Modules:** `store.py` (roster, `protected_agents`, `is_release_authorized` →
  delegates to the single `loop_exit_relay_authorized` resolver),
  `wrapper/loop.py` (`classify_loop_control`).
- **Roles are free-form labels**; `lead` and `operator_facing` (liaison) are
  *coordination* roles, not authority boundaries — after a restart, HOLD/GO and
  ownership are re-derived from the repo, the operator, and `sync`, not assumed.
- **Stand-down authority (v0.39.0):** idle agents *always keep listening*. A loop
  exits **only** on a typed `release`/`end` carrying a human-origin authority
  envelope from the authorized relay; prose, notes, and casual lead sign-offs
  never exit a loop. `release --relay-human` (relay a human stand-down) XOR
  `--emergency` (narrow lead override, must be reported to the operator), both
  require `--reason`. The authorized relay = the operator-facing liaison if
  configured, else the sole active lead; **fails closed** otherwise.
- **Why:** agents kept going offline because a lead's casual prose ("stand down
  for the night") leaked outside the typed-signal channel. The rule "only the
  human stands an agent down, relayed by the lead" applies principle #1 (bodies
  are data) and #8 (human authority is typed) to liveness. See decision D-7.

### 4.3 Epochs & barriers
- **Modules:** `store.py` (epoch), `cli.py` (`barrier`, `check --epoch`).
- A monotonic epoch marks "the world changed" (a broad change, a release). Lanes
  and knowledge notes stamp the epoch / registry hash so staleness can be
  detected. `check` is the pre-action gate (rescinded? stale? HOLD?).
- **Why:** cheap, durable "is my assumption still current?" without locks.

### 4.4 The assurance arc — making unsafe "done" hard
The thesis: **a confident-but-wrong "GO" is worse than a slow "HOLD."** The arc
adds typed evidence and fail-closed verdicts to the human/agent claim of "done."
- **`gate` (`gates.py`, v0.32.0):** named gates with HOLD/GO; a `severity=blocker`
  gate can only be green from `automation_ci` or an operator waiver; typed
  review-result evidence (`risk_class`, `release_blocker`, `tests_referenced` vs
  `tests_executed`, `na_reason`). A corrupt/unreadable gate state HOLDs.
- **`close` (`close.py`, v0.33.0):** milestone-close with a *pure* `compute_verdict`
  and stable HOLD codes; lens authorization; blocker-gate-must-be-green.
- **Sign-offs (v0.34.0):** specialist sign-off by risk class, `signoffs.json`,
  distinct-agent counting, override-with-reason.
- **Devkit skills (v0.35.0):** generic review/tester skill pack (risk-class
  lenses, failure-injection, contract-drift, NA-needs-reason).
- **Ephemeral adversarial reviewers (v0.36.0):** the lead can launch one-shot
  fresh agents for an independent review; **evidence-only** (never counted
  sign-offs — protects sign-off integrity); disabled by default.
- **Why:** dogfooding showed real false-GO/TOCTOU/honesty bugs; the arc is the
  process that catches them. (It works: the 2026-06-28 fresh audit used exactly
  this posture to find the gate/lane authority bugs in `docs/ISSUES.md`.)

### 4.5 The middle tier — ownership & durable memory
Built around **one** shared ownership registry so lanes and knowledge don't grow
two parallel ownership concepts.
- **Domains (`domains.py`, v0.31.0):** the durable registry `.agenttalk/domains.json`
  (public noun = `domain`; survives `reset`). Owners/reviewers/curators, glob
  ownership (segment-aware, casefold-consistent), a registry hash used as the
  staleness keystone. `domain list/show/check-path/validate`.
- **Lanes (`lanes.py`, v0.37.0):** a lane scopes an assignee to a domain (+ path
  subset). `lane deliver` gives a point-in-time GO/HOLD that the diff is
  in-bounds, current (epoch/registry), merge-tree-clean (honest-degraded — never
  *infers* clean), and gate-clean. Pure `compute_verdict` core + a thin git
  adapter (so verdict logic is unit-testable with no live repo); durable delivery
  artifact written before the lane is cleared; fingerprint re-validated under lock.
- **Knowledge (`knowledge.py`, v0.38.0):** an append-only **pointer** layer
  (`notes.jsonl`), latest valid event by `(domain_id, key)`. Capture-open +
  curate-gated (anyone publishes `uncurated`; owners/curators/lead verify/
  supersede/retract). **Anchor-relative staleness** (hard-stale only when the
  *anchor* changed `verified_against_sha..HEAD`; HEAD merely moving = caution).
  Pointer-not-mirror: bodies are byte-capped, untrusted, and carry only the
  insight not already in the artifact. `roster --expertise` derives from domain
  roles + lane-delivery history.
- **Why:** a project needs ownership (who may change/approve what) and memory
  (the durable *why* behind seams/gotchas) that survive resets and don't rot.
  Pointers + anchor-relative staleness keep memory trustworthy instead of a
  stale wall of text.

### 4.6 Unattended operation — supervisor & wrapper
- **Supervisor (`supervisor.py`):** generates a PowerShell monitor that launches/
  relaunches agents and watches **heartbeat staleness** for liveness (not fragile
  PID/brain discovery). Protected agents (all leads + the liaison) are never
  auto-killed. Restart-with-context via `request-restart`.
- **Wrapper (`wrapper/`):** `agenttalk wrap` runs an agent's listen loop *for* it
  (`loop.py`), giving visibility, a working-turn heartbeat, degraded-output
  detection (`degraded.py`), and session continuity (`session.py`; codex
  `thread_id` / claude session-id). The wrapped **model is a pure per-turn
  handler** — the wrapper owns the loop and loop-exit, so a resumed session
  re-enters listening regardless of the model (principle #7).
- **Why:** real hangs and resume-wake churn happened in the field. Heartbeat
  liveness + wrapper-owned loop is the robust unattended answer; wrapped is the
  recommended supervised archetype.

### 4.7 Supporting modules
- `signing.py` — optional HMAC message signing (constant-time, length-floored);
  when enforced, unsigned/invalid messages are refused (fail closed).
- `capacity.py` — advisory per-agent headroom/context snapshots (planning hint
  only; never blocks protocol).
- `doctor.py` — diagnostics: invalid/torn message and knowledge lines, resolved
  supervised CLI path/version, etc.
- `_atomic.py` — the atomic-write primitive (see principle #3).
- `web.py` / `dashboard` / `serve` — read-only views.
- `codex_config.py`, `install_skills.py` — environment/skill setup.

## 5. Design decisions & rationale (ADR-lite)

Append new decisions here (dated). Keep each short: decision, why, alternatives.

- **D-1 Filesystem bus, no server/DB.** *Why:* zero infra, inspectable,
  restart-recoverable, no daemon to crash. *Rejected:* a socket/daemon broker
  (fragile, stateful, another crash surface).
- **D-2 Bodies are data, never instructions.** *Why:* untrusted-input safety +
  determinism; prose can't move protocol state. *Rejected:* convenience parsing
  of natural-language commands from bodies.
- **D-3 Fail closed everywhere.** *Why:* a false GO is worse than a slow HOLD.
  *Rejected:* "assume pass if state missing/unreadable" (this is exactly the bug
  class the audit found in `gates.py`).
- **D-4 Advisory, not authz.** *Why:* honest about what a coordination tool can
  guarantee; the real boundary is Git/OS. *Rejected:* claiming lanes/leases are
  security.
- **D-5 One unified domain registry for lanes + knowledge.** *Why:* avoid two
  ownership models drifting apart. *Rejected:* per-feature ownership.
- **D-6 Anchor-relative knowledge staleness.** *Why:* HEAD-relative would empty
  the knowledge layer on every unrelated commit (noise → ignored); content-changed
  detection keeps notes trustworthy. *Rejected:* "stale on any commit".
- **D-7 Stand-down is human-origin only; idle = always listening.** *Why:* a
  lead's prose kept taking agents offline; loop-exit must be a typed,
  human-authorized envelope. Lead *relays*, never originates a normal stand-down;
  narrow emergency override must be reported. *Rejected:* "any lead/any protected
  agent can release" (too broad; diverged from `is_release_authorized`).
- **D-8 Capture-open + curate-gated knowledge.** *Why:* the discoverer of a
  gotcha is rarely the curator; make capture cheap, make the *authoritative* set
  curated. *Rejected:* curator-only publish (bottleneck) and open-publish (noise).
- **D-9 Ephemeral reviewers are evidence-only.** *Why:* counting one-shot
  spawned agents as sign-offs would let the lead manufacture approvals.
- **D-10 Per-phase cadence with isolated worktrees.** *Why:* concurrent builders
  on one working tree collide on `git checkout`; each builder uses an isolated
  `git worktree`. (See §6.)

## 6. How we work (process)

- **Per-phase cadence:** architect designs → lead gates the design → builder
  implements in an **isolated git worktree** → cross-review (≥2 distinct
  reviewers) → lead gate → fast-forward merge → release.
- **Lead gate (the bar to merge/ship):** in an isolated worktree off the candidate
  SHA — `ruff`, `bandit -r src -x src/agenttalk/skills`, `git diff --check`, and
  full `pytest` on **Python 3.10 and 3.14**.
- **Release ritual:** bump `src/agenttalk/__init__.py` + `pyproject.toml`; add a
  `CHANGELOG.md` section; update README install pins; commit via `git commit -F`
  (never `-m` for multi-line — PowerShell native-arg trap); tag; push; `gh release
  create`; watch CI.
- **We dogfood the assurance arc on ourselves:** our own work is reviewed and
  gated by the same gate/close/evidence machinery this tool provides.

## 7. Where things live (module map)

| Concern | Module(s) |
|---|---|
| Bus, mailbox, cursors, config, locking, validation, authority | `store.py` |
| Threads / who-owes-whom | `threads.py` |
| Atomic writes | `_atomic.py` |
| Assurance gate / typed evidence | `gates.py` |
| Milestone close / sign-offs | `close.py` |
| Ephemeral reviewers | `ephemeral.py` |
| Domain registry | `domains.py` |
| Lanes (deliver-gate) | `lanes.py` |
| Knowledge (durable memory) | `knowledge.py` |
| Unattended supervisor | `supervisor.py` |
| Wrapper (loop, run, session, degraded, adapters, recv) | `wrapper/` |
| Signing | `signing.py` |
| Capacity hints | `capacity.py` |
| Diagnostics | `doctor.py` |
| CLI verbs | `cli.py` |

For the current state of work and known gaps, see **`docs/ISSUES.md`**.
