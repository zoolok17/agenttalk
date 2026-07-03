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
  configured, else the sole active lead; **fails closed** otherwise. The CLI,
  store helpers, and wrapper loop all use the same resolver because divergent
  loop-exit authority is itself a liveness bug: no liaison, multiple leads, or
  no sole lead means deny rather than guessing.
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
  `tests_executed`, `na_reason`). Review prose helps humans, but gates consume
  typed metadata; a corrupt/unreadable gate state HOLDs.
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
  Shared-path approvals are registry-entry authority, not raw path-prefix
  authority: a touched shared path is cleared only when **every** matching shared
  entry has a fresh approval recorded against that entry by an authorized approver
  (a close lead or that entry's default approvers). There is no winner-picking
  between overlapping entries — that ordering heuristic was twice unsound — so a
  path governed by two entries must satisfy both; duplicate normalized shared
  globs are rejected at validation.
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
  The domain registry is the authority spine for both: lanes answer "may this
  diff move now?", while knowledge answers "what durable context should the next
  worker load?" Pointers + anchor-relative staleness keep memory trustworthy
  instead of a stale wall of text.

### 4.6 Unattended operation — supervisor & wrapper
- **Supervisor (`supervisor.py`):** generates a PowerShell monitor that launches/
  relaunches agents and watches **heartbeat staleness** for liveness (not fragile
  PID/brain discovery). Protected agents (all leads + the liaison) are never
  auto-killed. Restart-with-context via `request-restart`. Windows launches default
  to `window_style=hidden` (global setting, per-agent/profile override) so a
  supervised fleet does not cover the operator's desktop; hidden wrapped agents
  also pass a no-child-window marker into the wrapper so its CLI child uses
  Windows `CREATE_NO_WINDOW`.
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
  security. *Lead-loop scope (Slice 1/WP1):* the single-consumer guard and the
  plaintext `lease_id` owner-bypass are advisory coordination INSIDE the trusted
  `.agenttalk/state` domain, NOT authorization. Any process that can read/write
  `.agenttalk/state` can read the lease, forge a marker, or tamper with the
  coordination files; the guard stops accidental double-consumption by cooperating
  windows, not a hostile local process. (See D-2: message bodies, and here the lease
  files, are untrusted data, never a security boundary.)
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

- **D-11 Shared lane approval requires approval from EVERY matching entry.** *Why:*
   a broad approval for `shared/**` must not clear a sensitive nested path such as
   `shared/secret.sql` when the registry assigns that nested entry a different
   approver set. A touched shared path is cleared only when *every* matching shared
   entry has a fresh, valid approval recorded against that entry by an authorized
   approver; otherwise HOLD. Verdicts revalidate persisted approvals against the
   current epoch/registry, and validation rejects duplicate normalized shared
   globs. *Rejected:* raw path-prefix approval, first-match-wins, and
   most-specific-entry-wins — picking a winner among overlapping globs imposes a
   total order on a partial order and was twice proven unsound (a wrong choice on
   a security boundary is a bypass). Requiring all matching entries removes the
   ordering question entirely and is provably fail-closed (D-3). *Evolution:* the
   v0.40.0 fix iterated prefix-match → most-specific tuple → all-matching as
   review reproduced progressively deeper bypasses; all-matching ended the class.

- **D-12 Lead-loop lease steal uses a CONFIRMED-dead tri-state liveness.** *Why:*
  the managed lead-loop lease (Slice 1) must let a crashed controller be taken over
  at once - gating recovery on the full TTL (900s default) would strand the team
  mailbox and defeat supervisor auto-recovery. But the immediate steal must NEVER
  displace a *live* controller. The ordinary `_process_alive` probe is fail-quiet
  (it collapses *uncertain* - access-denied, ambiguous OpenProcess failures, any
  exception - into "not alive"), so "not alive" is NOT proof of death. *Decision:*
  a dedicated tri-state probe `_process_liveness` returns ALIVE / DEAD / UNKNOWN,
  where **DEAD is only a DEFINITIVE not-running signal** (POSIX `ESRCH`; Windows
  `GetExitCodeProcess != STILL_ACTIVE`, or `OpenProcess` failing
  `ERROR_INVALID_PARAMETER`). The lease steal, the `armed` detector, AND the guard
  all use it: a CONFIRMED-DEAD owner is stealable/unarmed/unguarded IMMEDIATELY
  regardless of expiry; an ALIVE *or* UNKNOWN owner is treated as probably-alive -
  armed, guarded, and stealable only once the lease is EXPIRED *and* its heartbeat
  is stale (a stuck controller). So a long healthy turn is never stolen, and a
  fail-quiet/uncertain probe can never immediate-steal a live controller; a
  genuinely-dead-but-unprobeable owner still recovers via the expiry+heartbeat
  path. *Invariant:* the three authority decisions share one tri-state, so the
  detector is the EXACT complement of stealability - for a present managed lease,
  `armed == not stealable` for every case (alive, dead, *and* unknown). *Rejected:*
  basing immediate steal on `not _process_alive(pid)` - that turns a fail-quiet
  probe false-negative into a false steal of a live owner. *Evolution:* expiry-for-
  ALL-steals -> reviewer-1 reproduced a dead-owner-within-TTL limbo (narrowed to
  dead-owner-immediate) -> codex reproduced a false-steal of a live owner under an
  uncertain probe (narrowed "dead" to the CONFIRMED tri-state, Option A across
  steal+armed+guard).

- **D-13 Corrupt-config coercion: `isinstance`, never falsy-only `or {}`.** *Why:* a
  per-agent `supervisor.json` entry that is a TRUTHY non-dict (an operator typo like
  `{"agents": {"beta": "wrapped"}}`) is not rescued by `(x or {}).get(...)` - the
  string slips through and `cfg_agent.get(...)` raises `AttributeError`, crashing the
  reader. *Decision:* every per-agent config reader coerces to `{}` only when the
  value `isinstance(..., dict)`, never via falsy-only `or {}`. Applied uniformly at
  `lead_loop_runtime.resolve_timing`, `supervisor.resolve_stuck_after`,
  `supervisor.resolve_dead_letter_caps` (config / cfg_agent / nested `dead_letter`),
  `supervisor.session_args`, and `cmd_wrap`'s extraction - including the pre-existing
  v0.41.0 dead-letter/wrap sites surfaced while closing the class. All fail safe to
  the defaults; `claude_permission_mode` already used the `isinstance` form.
  *Rationale:* a single per-agent typo must NEVER crash `status` / `doctor` /
  `supervise --report` / `wrap --loop` startup - config is untrusted data (D-2), so a
  malformed entry degrades to the default, it does not take down the tool. *Surfaced
  by:* WP1 (lead verify P2 = the resolve_timing crash; codex review = the wrap/
  dead-letter sibling; dev-2 proactive sweep = session_args).

- **D-14 The cadence tick is a SYNTHETIC wrapper-owned event, isolated from the
  dead-letter path.** *Why:* the managed lead-loop controller (Slice 2) must do
  PROACTIVE work when the bus is quiet (nudge stalled outbound threads, surface
  dead-letter / unrouted-escalation to the operator) - but a proactive sweep is NOT a
  bus record, and treating it as one would be a correctness disaster: it would advance
  the consume cursor (skipping a real message that arrives mid-sweep) or feed the v0.41
  poison-message machinery (a "failed sweep" would dead-letter, attributing controller
  trouble to an innocent message). *Decision:* the cadence tick lives in the
  *timeout/idle branch* of the SAME `_run_continuous` loop (no second consumer/thread),
  and it NEVER calls `record_attempt_start`, NEVER advances the cursor, and NEVER enters
  the dead-letter path. It is gated by a controller-owned single-writer cadence state
  (`state/<agent>.lead-loop-cadence.json`, reset-cleared like the lease; the dead-letter
  SINK is elsewhere and survives reset) that drives due-ness, reminder dedup (once per
  `(request_id, last_msg_id)`), and escalation dedup. A sweep fires a model turn ONLY if
  a bounded, read-only snapshot has actionable items (no actionable items -> no model
  turn). A FAILED sweep is **controller-HEALTH** trouble, never message poison: it backs
  off (exponential), withholds the heartbeat so the supervisor notices, and after a
  threshold escalates ONCE to the operator. *Invariant:* a cadence tick can SEND but can
  never advance the lead-loop cursor unless it was handling a real record (it never is).
  *Rejected:* a second consumer thread for the sweep (a duplicate consumer races the
  bus, the exact failure the lease prevents); a universal "tick failed -> dead-letter"
  (conflates controller health with message poison). *Built by:* WP3.

- **D-15 The operator<->lead-loop relay is a THIN typed wrapper, not a new transport or
  kind.** *Why:* a split-identity lead-loop runs headless - the operator speaks through
  a liaison. Two crossings need to be auditable: the operator's ANSWER to an escalation,
  and a SPONTANEOUS operator instruction. The temptation is a new message kind or a
  side-channel; both fragment the bus (threading, validation, and the supersession/ack
  machinery all key off the existing kinds + meta). *Decision:* `agenttalk relay` reuses
  the existing `reply`/`send` plumbing and adds only META: (1) `relay operator-answer
  --to-request <rid>` VALIDATES that `<rid>` is a *pending* `needs_operator` opener
  addressed to *this* liaison, then sends a normal thread reply stamped
  `operator_answer=true` + `operator_origin=<liaison>` so it routes back to the asking
  lead-loop's own mailbox and flips the thread to `operator_state=answered`; (2) `relay
  operator-command` sends a `question`/`message` stamped `operator_command=true` +
  `operator_origin`, minting a fresh `request_id` for the question (a caller-supplied
  `--meta request_id` is REFUSED - the command owns its correlation id), INFERRING `--to`
  only when exactly one managed lead-loop exists (else requiring it; an EXPLICIT `--to` may
  be any roster agent - the managed-only restriction is for inference, not the explicit
  choice), and FAILING CLOSED unless the sender is the current operator-facing liaison (an
  `--override --reason` is the only, audited, exception). Both handlers are AUTHORITATIVE
  for the reserved control/audit/routing meta: they SCRUB any caller-supplied
  `operator_*` / `needs_operator` / `broadcast_id` / `in_reply_to` / `target_msg_id` and
  re-stamp only what each command owns, so a caller `--meta` can never forge an audit
  marker (e.g. a fake `operator_command_override`) or graft routing onto a relayed message.
  The lead-loop->operator direction stays the existing `escalate` (a `needs_operator`
  question). *Liaison-down:* a relayed message is an ordinary durable
  bus record - it QUEUES in the target's inbox whether or not the target is up; a pending
  operator answer is represented by the OPEN THREAD, never by blocking the controller
  (the cadence sweep treats `operator_pending` as tracked-not-blocking, D-14); with no
  liaison/lead resolvable the controller does not spin - it surfaces via doctor /
  cadence-health and keeps handling work that needs no operator input. *Rejected:* new
  `operator_answer`/`operator_command` KINDS (the bus skips unknown kinds and every
  thread/validation path would need teaching); liaison "memory" instead of a mechanical
  relay (unauditable, and it puts operator authority in an agent's prose). *Honest limit:*
  `operator_origin` is an auditable trusted-team assertion, NOT cryptographic proof the
  human spoke (D-4 / SECURITY.md). *Built by:* WP4 (completes the Slice 2 split-identity
  lead-loop: WP1 authority foundation, WP2 controller, WP3 cadence, WP4 relay).
- **D-16 The operator attention queue is a DERIVED read-only projection + a durable
  disposition log — no new work objects, no new message kind (0.56.0).** *Why:* the signals
  a human must act on already exist (pending `needs_operator` threads, config-blocked holds,
  dead letters, gate/close HOLDs, unarmed lead-loops); the gap was that they were scattered
  and that a decision to defer/dismiss one didn't *stick*. `agenttalk attention` PROJECTS
  those sources into one ranked view and records operator decisions in an append-only
  `attention/dispositions.jsonl` (latest-valid-by-(item, action-family), fsync under the
  store lock, skip-invalid on read, preserved by reset — the knowledge/dead-letter log
  pattern). Two invariants make it safe to trust: (1) dispositions are **snapshot-bound** —
  an item's `source_hash` folds its identifying *content*, not just its key, so a defer/dismiss
  hides it only while the situation is unchanged; a different config fault for the same agent,
  or an expired defer, resurfaces (D-3 fail-safe: when in doubt, surface). (2) the projection
  is **fail-safe and cheap** — each source read is independently guarded (a failure becomes a
  bounded `source_error` row, never a blank queue), it reuses the pure derivation helpers
  rather than scraping doctor/status text, and it does no git/lane recompute on the default
  path (gate 8). `dismiss` is refused for blocking sources (fail-closed: blockers get
  repaired/answered/deferred, never silenced); disposition authority is the operator-facing
  liaison / sole-lead resolved from identity (no `--by`), matching the single-voice operator
  contract. Dead-letter `resolve` is a sibling operator decision (payload-preserving) whose
  authority is the *central* log, with a best-effort `.resolved.json` sidecar for copied
  sinks. *Rejected:* a new message kind or work object (the bus skips unknown kinds and every
  thread/validation path would need teaching; the queue is a *view*, not a new noun);
  mirroring flat `meta.*` escalation fields (one canonical nested `meta.attention` block
  avoids drift); recomputing gates/lanes on view (cost + a coupling that would make a heavy
  source stall the whole queue). *Honest limit:* the queue reflects what the sources report;
  a disposition is an audit assertion by the resolved actor, not cryptographic proof (D-4).
  *v1 scope:* no bulk/group dispositions; dedupe is display-only; capacity/close-hold rows
  are wired fail-safe but surface only on a cheap threshold-tripped read.

- **D-17 In-turn liveness is a BOUNDED work-heartbeat ticker, never an unbounded one —
  and it does not write health.** *Why:* during a long non-streaming turn the wrapper's
  idle heartbeat is blocked and the framework heartbeat stamps only on streaming progress,
  so a wrapped Claude (stuck_after=180s) could be false-STUCK_RECOVERed mid-turn during
  legitimate >180s silent work. `wrapper/work_heartbeat.py` stamps the SAME supervisor
  heartbeat (the lead-loop's combined renew-then-stamp included) while the per-turn child
  is alive, but only until `max_turn_seconds` (default 900s for wrapped Claude) — past the
  cap only real progress refreshes liveness and the supervisor's stale recovery applies, so
  the worst-case silent-hang recovery is `max_turn_seconds + stuck_after_seconds`, never
  masked forever. **Semantics change (deliberate, bounded):** with the ticker live, a fresh
  in-turn heartbeat means "wrapper + child alive", not "turn observably progressing"; a
  wedged stream reader with a live child is masked only until the cap. The ticker does NOT
  refresh health: the planner's health-delays-recovery branch would otherwise stretch the
  true recovery bound past the documented cap+stale. HARD invariant: one lock guards stamp
  execution and stop state — `stop()` synchronizes with any in-flight stamp before drive()'s
  failed-turn `clear_heartbeat`, so a failed turn can never end with a fresh ticker stamp.
  *Rejected:* enabling for wrapped Codex / changing codex `stuck_after` or the
  watchdog-preemption math in the same release (a coupled two-knob safety change; the
  supervisor would be trusting a config-says-live ticker without runtime evidence — the
  0.58.2 drift class); a supervisor-visible effectively-live predicate as a planner input in
  v1 (diagnostics only); one-shot default-ON (the ephemeral lifecycle is
  deadline/completion/process-liveness driven — no stale-heartbeat consumer, so ticker
  writes would be dead liveness). *Honest limit:* enabled-config vs running-ticker drift is
  surfaced only via the diagnostics status record, not enforced, in v1.

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
| Operator attention queue + dispositions | `attention.py` |
| Unattended supervisor | `supervisor.py` |
| Wrapper (loop, run, session, degraded, adapters, recv) | `wrapper/` |
| Signing | `signing.py` |
| Capacity hints | `capacity.py` |
| Diagnostics | `doctor.py` |
| CLI verbs | `cli.py` |

For the current state of work and known gaps, see **`docs/ISSUES.md`**.
