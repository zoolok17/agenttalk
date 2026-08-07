# agenttalk — design & rationale

This document explains **what agenttalk is, how it is built, and why** the major
design decisions were made. It is the orientation map for anyone (human or agent)
working in this codebase: the code and tests are the source of truth for *how it
behaves*; this doc is the source of truth for *why it is shaped this way*.

Companion docs:
- `docs/ROADMAP.md` — the official product roadmap + feasibility verdict (where this is going).
- `docs/ISSUES.md` — the living tracker (work in flight, known limitations, audit findings).
- `docs/ASSURANCE.md` — per-release GOOD/ROBUST/SECURE attestation + the codebase security posture.
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

3. **Durable writes are atomic; serialization is resource-specific; readers are
   fail-safe.** Mutations go through `_atomic.write_text` (temp + fsync +
   `os.replace`, with a latched sandbox-direct fallback). Hardened
   cross-process locks cover shared config, retirement versus final send
   publication, launch requests, and waiting markers; closes and lane cleanup
   use their own serialization boundaries. Per-agent cursor and threadstate
   read-modify-write sequences remain unlocked under the one-consumer model.
   Lock publication rejects stale generations and unsafe
   non-regular/reparse paths, but it is not a same-user security boundary.
   JSONL append owners serialize and fsync complete records; readers isolate
   malformed physical lines, surface them, and retain later valid records.
   Cursors bias toward re-delivery rather than skipping a message.

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
   listen loop. Cursor and threadstate writes are atomic, but their surrounding
   read-modify-write sequences are not cross-process serialized. Stacked
   listeners can lose state, execute the same inbound work, and emit conflicting
   replies. `status`/`doctor` warn, and the design avoids second consumers (e.g.
   the wrapper owns the loop and the model is a pure per-turn handler).

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
- `supervisor-state.json` + `.bak` — script/runtime session state with a
  validated previous-generation recovery copy (read-only fallback never repairs
  a corrupt primary implicitly).
- `knowledge/notes.jsonl` — append-only durable team memory (survives `reset`).
- `attention/dispositions.jsonl` — append-only operator decisions over the
  derived attention queue (survives `reset`).
- `lane-deliveries/` — durable committed lane delivery artifacts;
  `.prepared/` contains non-consumable transaction staging.
- `dead-letter/<agent>/` — quarantined poison-message payloads and sidecars
  (survives `reset`; requeue copies, it does not rewind).
- `state/intents/active/` — current-session dashboard intents (cleared by
  `reset`; queued control from an old session must not fire in a new one).
- `control-audit/` — terminal dashboard-control audit records (survives
  `reset`).
- `sessions/` — transcripts.

**Durability boundary (load-bearing):** `reset` clears `messages/` and
`state/` (active runtime), but **preserves** `domains.json`, `knowledge/`,
`attention/`, `closes/`, `gates.json`, `lane-deliveries/`, `dead-letter/`,
`control-audit/`, and `sessions/`. Getting this set wrong is catastrophic, so
it is asserted by the end-to-end regression test.

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
- Response state is a typed protocol, not free-form metadata. A present
  `review-result` status must be `approved|rejected|needs-info`; only the first
  two terminate review. A present `proposal-response` status must be
  `accepted|rejected|countered`, all terminal. Missing status remains readable
  for legacy history but is nonterminal; invalid present status is rejected at
  write time and skipped during derivation.

### 4.2 Roster, authority & stand-down
- **Modules:** `store.py` (roster, `protected_agents`, `is_release_authorized` →
  delegates to the single `loop_exit_relay_authorized` resolver),
  `wrapper/loop.py` (`classify_loop_control`).
- **Roles are free-form labels**; `lead` and `operator_facing` (liaison) are
  *coordination* roles, not authority boundaries — after a restart, HOLD/GO and
  ownership are re-derived from the repo, the operator, and `sync`, not assumed.
- **The live roster is the source of truth for membership/roles/liaison/groups**;
  these change over time, so every dispatch, relay, and recipient set resolves them
  from the live store, never from a memorized or handed-off snapshot. A manual
  bootstrap/rejoin may use `sync`; inside a wrapped `--loop` turn (where `sync`/`threads`/
  `drain`/`recv`/`wait`/`ack` are the wrapper's, not the child's) act-time resolution uses
  read-only `roster`/`whoami` plus the validated envelope. `send` rejects an off-roster/
  retired recipient, but a stale roster still misroutes to a wrong still-active recipient or
  misses a newly-added one (D-25, corollary of principle #1).
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
  and stable HOLD codes; lens authorization; blocker-gate-must-be-green. Existing
  records update under a per-close lock with mandatory generation plus immutable
  instance preconditions. The instance prevents delete/recreate ABA; creation is
  exclusive, force-open replaces under lock, and legacy records upgrade under
  lock without an unchecked overwrite. Publish recomputes impure evidence at the
  serialization boundary. A requested barrier is bound to close id, instance,
  revision, and generation so send/stamp failure resumes idempotently; duplicate
  matching barriers fail closed.
- **Sign-offs (v0.34.0):** specialist sign-off by risk class, `signoffs.json`,
  distinct-agent counting, override-with-reason. Boolean policy fields require
  real JSON booleans, counter ids are globally unique within a close across all
  lenses, and close shares the gate
  risk vocabulary (`none`, `unknown`, `release`, `device`, `accessibility`,
  `security`, `performance`, `persistence`, `docs-contract`, `quality`, plus
  namespaced project extensions).
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
  adapter (so verdict logic is unit-testable with no live repo). Delivery is a
  recoverable two-phase transaction: signed `prepared` evidence is
  non-consumable, a lane generation/instance checkpoint records
  `publish_pending`, terminal inputs are rebound, and only then is a `committed`
  artifact published and marked consumable. Retry resumes the same transaction;
  force, abandon, and reassign cannot bypass an incomplete publication.
  Shared-path approvals are registry-entry authority, not raw path-prefix
  authority: a touched shared path is cleared only when **every** matching shared
  entry has a fresh approval recorded against that entry by an authorized approver
  (a close lead or that entry's default approvers). There is no winner-picking
  between overlapping entries — that ordering heuristic was twice unsound — so a
  path governed by two entries must satisfy both; duplicate normalized shared
  globs are rejected at validation.
  Worktree isolation extends this lane layer: a release-class `lane assign`
  provisions a managed `git worktree` and `lane/<id>` branch. `--no-worktree`
  is available only for an explicitly advisory lane with a recorded reason;
  the waiver authority is `advisory_only`, does not trust role labels, and can
  never satisfy release isolation. Mutating git goes through
  `_git_write` (argv-list, prompt-disabled env, bounded timeout, `--` before
  positionals, full-SHA base). `lane deliver` derives HEAD from the registered
  worktree, signs canonical worktree/branch provenance into the delivery artifact,
  and keeps the branch so `close` can re-verify after the worktree is removed.
  Publication and teardown are separate checkpoints: committed evidence remains
  valid during `cleanup_pending`/`cleanup_failed`, and cleanup is recoverable
  without holding the broad config lock across expensive Git work.
- **Knowledge (`knowledge.py`, v0.38.0):** an append-only **pointer** layer
  (`notes.jsonl`), causally folded by `(domain_id, key)`. Capture-open +
  curate-gated (anyone publishes `uncurated`; owners/curators/lead verify/
  supersede/retract). **Anchor-relative staleness** (hard-stale only when the
  *anchor* changed `verified_against_sha..HEAD`; HEAD merely moving = caution).
  Pointer-not-mirror: bodies are byte-capped, untrusted, and carry only the
  insight not already in the artifact. `roster --expertise` derives from domain
  roles + lane-delivery history. Lessons are a `type=lesson` record in the same
  ledger: accepted lessons can be surfaced in `sync`/`onboard` as advisory
  process memory, but they do not authorize, block, or replace tests/skills/gates.
  The default retrieval view contains both pointer notes and lessons; a single
  selector applies scoped-domain freshness, filters/search, ordering, then
  per-section caps. New curation rows bind a prior event and a canonical immutable
  payload, while legacy rows remain readable when their payload matches prior state.
- **Why:** a project needs ownership (who may change/approve what) and memory
  (the durable *why* behind seams/gotchas) that survive resets and don't rot.
  The domain registry is the authority spine for both: lanes answer "may this
  diff move now?", while knowledge answers "what durable context should the next
  worker load?" Pointers + anchor-relative staleness keep memory trustworthy
  instead of a stale wall of text.

### 4.6 Unattended operation — supervisor & wrapper
- **Supervisor (`supervisor.py`):** generates a PowerShell monitor that launches/
  relaunches agents. Manual listeners use heartbeat staleness for liveness.
  Wrapped listeners additionally require one strictly parsed
  `wrapper-runtime.json` lifecycle record: only validated idle can be
  `HEALTHY_IDLE`; active work requires an independently discovered real CLI
  brain plus accepted adapter progress. Unknown or malformed evidence is
  non-green and never kill authority. Protected agents (all leads + the liaison) are never
  auto-killed. Restart-with-context via `request-restart`. Windows launches default
  to `window_style=hidden` (global setting, per-agent/profile override) so a
  supervised fleet does not cover the operator's desktop; hidden wrapped agents
  also pass a no-child-window marker into the wrapper so its CLI child uses
  Windows `CREATE_NO_WINDOW`. `supervisor-state.json` is shape-validated and
  backed by a validated `.bak`; a valid backup supports read-only recovery, while
  two invalid copies fail closed. Heartbeats beyond the configured future-skew
  bound cannot authorize freshness.
- **Wrapper (`wrapper/`):** `agenttalk wrap` runs an agent's listen loop *for* it
  (`loop.py`), giving visibility, a working-turn heartbeat, degraded-output
  detection (`degraded.py`), and session continuity (`session.py`; codex
  `thread_id` / claude session-id). The wrapped **model is a pure per-turn
  handler** — the wrapper owns the loop and loop-exit, so a resumed session
  re-enters listening regardless of the model (principle #7).
  The wrapper is also the single writer for the closed, atomic
  `wrapper-runtime.json` health record used by supervisor health/restart
  (#72). `progress_sequence` advances only for real accepted adapter events;
  heartbeat timer ticks are not progress. Durable progress records are
  interval-coalesced, while every accepted event advances the in-memory
  sequence and every terminal boundary forces a durable high-water write. The
  supervisor reserves the maximum coalescing interval before durable progress
  age can authorize recovery; progress staleness remains non-green immediately
  but cannot kill while heartbeat is fresh unless the per-turn watchdog is
  effectively live and its deadline-plus-margin floor is satisfied.
  Commit-vs-park (#73) independently trusts the validated bus, not this wrapper
  self-report. The two authorities are coherent: a #72 restart preserves the
  already-advanced bus cursor and cannot re-drive work #73 committed.
  Wrapper waiting markers carry unique generation tokens and teardown clears
  only a matching token, so an old loop cannot erase a replacement marker.
  The Windows per-turn watchdog kills a verified target with
  `os.kill(pid, SIGTERM)` instead of launching `taskkill.exe`; this is abrupt on
  Windows and eliminates the popup-producing `taskkill.exe` subprocess path.
  The production reporter's desktop-heap exhaustion diagnosis is plausible,
  not an upstream-confirmed root cause. Windows snapshot and start-time helpers
  run CIM through the selected Core host and fail open to unavailable/no-kill
  when selection validation fails; the recheck-to-kill PID ABA window remains,
  and leaf-first snapshot termination is not an atomic tree kill.
  These are follow-up hardening items, not blockers for the narrow fix.
- **Managed lead-loop controller (`lead_loop_runtime.py`,
  `lead_loop_cadence.py`):** the split-identity lead can own a durable lease,
  resolve one heartbeat-stale threshold for both stealability and visibility,
  and run bounded synthetic cadence ticks when the bus is quiet. Synthetic ticks
  never advance the cursor or enter the dead-letter path; they are controller
  health, not message poison.
- **Three liveness surfaces:** manual supervisor health uses heartbeat
  freshness, while wrapped supervisor health combines heartbeat, strict
  lifecycle phase, independently observed CLI-brain liveness, and adapter
  progress; top-level `health.py` is an
  advisory view of wrapper state (`idle_waiting`, `working_turn`, degraded,
  errored, unknown) that degrades safely and never authorizes a kill by itself;
  `deadman.py` is an independent mail-age SLO alarm over owed work, derived
  from the thread projector and not from supervisor state.
- **Why:** real hangs and resume-wake churn happened in the field. A wrapper
  can keep heartbeating after its CLI child dies or wedges, so heartbeat alone
  cannot authorize wrapped health. Strict turn lifecycle plus real child/progress
  observation closes that false-green; wrapped is the
  recommended supervised archetype.
- **Launch containment (v0.69.1):** a crashed wrapper that is not fully reaped must
  never be *replaced alongside* a survivor. Before spawning a replacement the
  supervisor re-snapshots and refuses to launch if a same-root, same-`--for`-agent
  wrapper (or its wait) still lives — or if the process snapshot is unavailable and a
  prior launcher may still be alive: it backs off and emits a deduped decision event
  rather than stacking wrappers (the self-DDoS failure mode). A barrier-held poll
  carries an explicit `barrier_state`, so it never fakes a launch or consumes a pending
  manual-restart request. `doctor` also warns about a stale generated `supervisor.ps1`
  that predates the per-project singleton lock.

### 4.7 Supporting modules
- `signing.py` — optional HMAC message signing (constant-time, length-floored);
  when enforced, unsigned/invalid messages are refused (fail closed).
- `capacity.py` — advisory per-agent headroom/context snapshots (planning hint
  only; never blocks protocol).
- `doctor.py` — diagnostics: invalid/torn message and knowledge lines, resolved
  supervised CLI path/version, etc.
- `_atomic.py` — the atomic-write primitive (see principle #3).
- `assurance.py` — standalone stdlib evidence producer run as
  `python -m agenttalk.assurance`; it emits scan artifacts and baselines, but
  does not decide GO/HOLD and is distinct from the `gate`/`close` verdict layer.
- `avatars.py` — allowlisted cosmetic avatar resolution, including the reserved
  `operator` principal; chosen ids resolve to known package assets only.
- `skill_currency.py` — mechanical source lint for bundled skills: CLI-token
  drift, frontmatter, and reviewed-against currency.
- `web.py` / `dashboard` / `serve` — loopback Team Console and JSON read views,
  plus an action-gated dashboard control surface behind `--enable-actions`.
- `codex_config.py`, `install_skills.py` — environment/skill setup.

### 4.8 Dashboard control plane (Architecture C)
- **Modules:** `web.py` (loopback server, CSRF/session, `/api/intent`,
  `/api/lead-chat`), `intents.py` (typed schema + executor), `store.py`
  (`write_intent`, `list_intents`, lead-chat identity), `web_static/console.js`.
- **Project identity and routing (v0.74.0):** every watched root has a
  stable path-derived `project_id`; labels are presentation plus read-only
  legacy compatibility, never write-routing identity, and duplicate basenames
  receive stable id suffixes. Selected-root responses return
  `root_info {project_id,label,path}`. The read resolver maps omission to
  `root[0]` and retains a unique display label as a legacy best-effort selector;
  other read selectors fail with HTTP 400 `bad_root`. The write resolver permits
  omission only for one served root and otherwise requires exactly one explicit
  full project id; it never accepts labels. Blank, repeated, unknown, ambiguous,
  or non-full write selectors fail with HTTP 400 `bad_root` before mutation.
  The top bar keeps project and path context visible; when CSS ellipsizes the
  path, the full value remains in text, title, and accessibility surfaces. A
  selector change pushes the project id into browser history; Back and Forward
  restore that selection and refetch its root-bound feeds. An actual root
  change clears root-bound caches, drill-ins, action state, queued-answer text,
  and generic/lead-chat composer drafts. Late responses are accepted only when
  their project id matches the current selection. This prevents accidental
  cross-root UI writes; it is not authentication or isolation from another
  local process.
- **Intent write spine:** for ordinary dashboard controls on `/api/intent`, the
  browser never calls `store.send()` directly. With `--enable-actions` off,
  `POST` stays disabled and no session token exists. With actions enabled, the
  browser may append a bounded, typed intent under `state/intents/active/` after
  loopback, Host, same-origin header, content-type, CSRF-token, session,
  body-size, and kind/payload checks. The supervised executor (`agenttalk
  supervise --drain-intents`) is the sole actor that claims intents, re-resolves
  authority server-side through `resolve_web_actor`, and performs those bus
  writes through normal store validation/HMAC paths.
- **Intent authority is re-derived at drain:** browser-provided origin, `from`,
  or `human_authorized` claims are diagnostics at most. Recipients, broadcast
  audiences, reply anchors, and escalation answers are resolved again from the
  current store when the executor drains. Any `lead_chat_send` record in the
  agent-writable queue is unconditionally denied; there is no authorized queued
  creation path for operator lead-chat sends.
- **Lead-chat direct-send exception:** v0.68.0 made the human operator a bus
  sender, but only as the reserved `operator` principal derived by
  `Store.operator_identity()` with no fallback. The operator principal is not a
  roster agent, is excluded from agents-only walks, and cannot be chosen by an
  agent. `/api/lead-chat` is not an intent path: after loopback, Host/Origin,
  content-type, CSRF, session, kill-switch, rate/cap, schema, lead-liveness, and
  reserved-principal checks, the handler itself sends on the single durable
  operator<->lead thread via `store.send(..., _allow_reserved_sender=True)` or
  answers a pending decision via `store.send_operator_answer_atomic`. Correct
  invariant: an operator bus-send happens only through this authenticated
  in-process route; queued `lead_chat_send` is denied as defense in depth.
- **Liveness render:** v0.69.0 keeps health semantics unchanged but fixes the
  dashboard display: an unwrapped agent with unknown health and a fresh heartbeat
  (within 120s) renders as `Active`; missing or stale heartbeat remains
  `Unknown`, and wrapped states keep their raw health labels.
- **Message avatars:** v0.69.3 renders a sender avatar beside each lead-chat
  message (operator on the right, lead on the left), reusing the allowlisted
  `avatars.py` records already carried in `/api/state`. Presentational only — no
  new server field or network path; message bodies stay `textContent`; avatar
  `src` comes only from allowlisted records (never a bus string); CSS is scoped
  to the lead-chat transcript (`.tc-lead-msg-row`), leaving the shared row style
  untouched.
- **Interactive-lead heartbeat:** v0.69.6 lets a human-launched operator-facing
  Claude window (which usually lacks `AGENTTALK_SELF`) stay honestly Active while
  working. `heartbeat --hook --fallback-for <lead>` resolves identity as `--for`
  → `AGENTTALK_SELF` → `--fallback-for` → silent no-op, so a shared project hook
  stamps each supervised worker as *itself* and only the env-less liaison falls
  through to the fallback; installed via `supervise --install-activity-hook
  --interactive-for <lead>`. No exemption — a stale/missing heartbeat still reads
  unavailable (fail-closed); it only supplies a better heartbeat path.
- **Honest ceiling:** these controls defend the local dashboard against
  drive-by localhost/CSRF/rebinding, keep ordinary dashboard authorization in
  the executor, and keep operator lead-chat sends inside one guarded route. They
  are not a cryptographic boundary against a fully privileged same-user local
  process that can write `.agenttalk/` or the repo.

### 4.9 Dead-letter poison quarantine
- **Modules:** `wrapper/loop.py` (drive outcome taxonomy and ceilings),
  `store.py` (attempt ledger and sink), `cli.py` (`dead-letter` verbs),
  `attention.py` (operator-visible notices and resolve disposition).
- A failed drive is classified as poison, infra, ambiguous, or config-blocked.
  The wrapper records a per-agent, per-message attempt ledger in
  `state/dead-letter-attempts/<agent>.json` before each drive, so a crash
  mid-turn still counts once on the next run. Torn or corrupt ledgers degrade
  low to zero attempts; they never false-quarantine a healthy message.
- `K_poison` bounds deterministic poison retries; `K_escalate` is the loud
  backstop for repeated ambiguous/unknown failure. Infra-dominant failures do
  not auto-dead-letter under the poison ceiling; they retry under backoff and
  escalate through the high-attempt path. Config-blocked is parked as operator
  work, not treated as message poison.
- Dead-lettering advances the cursor only after a payload is preserved under
  `.agenttalk/dead-letter/<agent>/`. `dead-letter requeue` injects a fresh
  message with a fresh id and fresh attempt count; it does not rewind the cursor
  or delete the original evidence. `dead-letter resolve` is an operator
  disposition; the payload remains for audit.
- **Failure classification is structured-first (0.69.2).** A global-infra
  label (which retries rather than dead-letters) requires a *structured* signal
  — a retryable rate-limit event, or an API status of 429/529/5xx/auth-outage —
  with legacy free-text markers (`timeout`/`unavailable`/`temporarily`/…)
  demoted to an ambiguous fallback used only when no structured fact exists, so
  a local error is not misread as a provider outage (`config_blocked` keeps
  first precedence). For diagnosis, a bounded **redacted** tail of the child's
  `stdout`/`stderr` is persisted with the dead-letter sidecar and shown by
  `dead-letter show`; it is never classification authority. Redaction strips
  `Authorization` bearer + assignment-style credentials before persistence, and
  the tail is byte-bounded by per-character UTF-8 cost (a multi-byte/non-BMP
  line cannot overflow the cap or split a character). Module: `redaction.py`.

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

- **D-18 Dashboard writes use Architecture C for general controls; lead-chat is
  the scoped direct-send exception.** *Why:* once the Team Console stopped being
  read-only, a direct browser->store write path for ordinary agent/dashboard
  actions would have made the web handler an authority boundary and would have
  repeated the drift classes the lane and operator-answer work already exposed.
  *Decision:* `/api/intent` is action-gated and can only append a typed intent
  envelope after loopback, session, CSRF, Host/Origin, content-type, size, kind,
  and schema checks. `agenttalk supervise --drain-intents` is the sole actor
  that claims, plans, revalidates, and performs those bus writes. The executor
  re-resolves the actor with `resolve_web_actor`, re-derives
  recipients/audiences/anchors from the current store, and treats all browser
  identity/human-authorized fields as audit data, not authority.
  *Operator-as-sender:* `/api/lead-chat` is the deliberate narrow exception: the
  authenticated handler sends directly as the reserved `operator` principal via
  `store.send(..., _allow_reserved_sender=True)` or answers via
  `store.send_operator_answer_atomic` after the full loopback/session/CSRF,
  kill-switch, rate/cap, schema, lead-liveness, and reserved-principal guard
  set. `Store.operator_identity()` has no fallback, the operator is kept out of
  agents-only walks, and a `lead_chat_send` intent found in the ordinary
  agent-writable queue is always denied. *Rejected:* direct POST endpoints
  calling `store.send` for general dashboard actions (too much authority in the
  web handler), routing lead-chat through an agent-writable queue kind (an agent
  could try to mint operator sends), a second control daemon (more lifecycle for
  the operator), and trusting a browser-supplied `from` or operator flag
  (body/meta are untrusted data, D-2). *Honest ceiling:* this is an
  authenticated local request boundary, not a cryptographic boundary against a
  same-user local process that can write `.agenttalk/` directly.

- **D-19 Dead-letter quarantine bounds poison retries without rewinding the bus.**
  *Why:* at-least-once delivery is correct for transient failure but unsafe for
  a valid message that deterministically crashes or wedges the wrapped model.
  Infinite retry starves the mailbox; blind cursor advance loses work.
  *Decision:* the wrapper classifies each failed drive with a `DriveOutcome`
  failure class. A write-ahead attempt ledger records one started attempt per
  message before the turn, then records poison/infra/ambiguous/config-blocked
  outcomes. Consecutive poison hits at `K_poison` move the payload into
  `.agenttalk/dead-letter/<agent>/` and then advance the cursor; repeated
  ambiguous/unknown hits at `K_escalate` take the same loud terminal path.
  Infra-dominant failures retry under backoff and escalate at the high-attempt
  path rather than being silently quarantined as poison. Requeue is recovery by
  copy: it sends a fresh message with a fresh id and attempt count, preserves
  the original sink payload, and never rewinds the cursor. *Rejected:* treating
  any failed drive as poison (outages would destroy valid work), never
  quarantining (one poison head can block the agent forever), and deleting the
  sink on requeue (destroys forensic evidence). *Honest ceiling:* classification
  is conservative but not omniscient; config-blocked and infra-heavy histories
  surface to the operator rather than pretending the tool can prove intent.

- **D-20 Shared control paths use scoped generation-aware serialization
  (2026-07-11).** *Why:* atomic replacement alone does not protect a shared
  read-modify-write sequence, and an old teardown can erase replacement state.
  *Decision:* hardened store locks serialize config mutation, retirement versus
  final send publication, launch-request creation/transitions/archive, and
  waiting-marker replacement/clear. Waiting instances carry immutable tokens,
  so stale teardown clears only its own generation. *Ceiling:* per-agent cursor
  and threadstate writes are atomic file replacements, but their surrounding
  read-modify-write sequences are not cross-process serialized. The supported
  model is one consumer per agent; duplicates can lose state and duplicate work.
- **D-21 A release barrier is a recoverable close-bound effect
  (2026-07-11).** *Why:* close state and bus publication cannot be one atomic
  filesystem transaction. *Decision:* persist GO with a unique binding to close
  id, instance id, revision, and generation; on retry, validate and reuse the one
  matching barrier or send once if absent, then stamp the epoch. Duplicate or
  mismatched effects HOLD. *Rejected:* stale pre-lock GO and sending a fresh
  unbound barrier after any partial failure.
- **D-22 Lane GO becomes consumable only after commit (2026-07-11).** *Why:* an
  artifact published before durable lane state could expose GO that teardown or
  state failure could not recover. *Decision:* prepared evidence is hidden,
  `publish_pending` is generation/instance-bound, terminal inputs are rebound,
  committed evidence is then published, and cleanup is a separately retryable
  checkpoint. Advisory no-worktree records can never stand in for release
  isolation.
- **D-23 Dashboard routes by stable project identity (2026-07-11).** *Why:*
  root-list indexes and duplicate labels are unstable and allow late responses
  to paint or mutate the wrong project after a switch. *Decision:* path-derived
  `project_id` is the write-routing identity and selected-root responses echo
  `root_info`. Read omission selects `root[0]`, with unique display labels kept
  only as legacy best-effort selectors; multi-root writes require exactly one
  explicit full id, never a label or omission. Bad, repeated, or ambiguous
  selectors fail before mutation, and the client
  generation-checks all root-bound responses. *Ceiling:* the id is not an
  authorization token.
- **D-24 Wrapped runtime config is injected into base_argv, fingerprinted from the
  EFFECTIVE argv, and adopts (never resets) an absent baseline (2026-07-14).** *Why:*
  per-agent model/effort must ride the same operator-authored launch tail the wrapper
  already owns, so injection mirrors `_inject_claude_permission_mode` and an explicit
  tail flag always wins (across every canonical and attached/`=` codex spelling). The
  restart-safe session fingerprint is computed from the model/effort actually present
  in the POST-injection argv, not the resolved config, so a hand-written tail model is
  tracked and a benign config edit that doesn't change the effective model doesn't
  reset. *Decision:* an ABSENT prior fingerprint is adopted silently (baseline); only
  PRESENT-but-different forces a fresh session (new claude uuid / cleared codex
  thread), so upgrading never wipes a live conversation — fail-closed still holds on
  the read side (a corrupt/unreadable session is already fresh). Dashboard status
  reads the wrapper session file through an allow-list projection that DROPS raw
  session/thread ids at the store boundary (D-2), and reset reasons map to a closed
  token set. *Ceiling:* effort validation is a per-CLI launch-time typo guard, not a
  per-model validator; injection covers `--loop` wrapped agents only.
- **D-25 Model/effort selection and agent-context lifecycle are operator/project
  POLICY, not core mechanism (2026-07-14).** *Why:* the core injects and fingerprints a
  per-agent model/effort (D-24) but does not — and should not — decide WHICH profile a role
  runs at, WHEN to reset a session, or WHEN to spawn a fresh independent reviewer; those are
  judgment calls that belong to the operator and the team's working policy (principle #5 —
  human authority stays outside the mechanism). *Decision:* the governance policy lives in
  the manuals (`docs/AGENT-MANUAL.md` §1, `docs/USER-MANUAL.md` §8) and the skills, not in
  code: configure STABLE role/task-class profiles and retune only on evidence (a change to the
  *effective* model/effort resets a present baseline's session, D-24); prefer a second
  INDEPENDENT lens over maxing one agent; treat Codex (shared load-balanced pool — cap/stagger
  concurrent high turns, vary effort not model) and Claude (weekly budget — sonnet workhorse,
  reserve opus) asymmetrically; reset context on scope discontinuity or contamination (not
  electively at task end; mid-task only when necessary, always checkpoint first) while noting a
  reset is NOT independence; use a fresh, preferably different-model-family agent for an
  independent review, giving it scope but not the build reasoning. The live roster/config is
  the source of truth; a cached or handed-off roster is stale (corollary of principle #1 /
  D-2). *Ceiling:* this is discipline, not an enforced boundary — the core validates neither a
  chosen profile against a task nor the effort token against a model at request time; `send`
  rejects an off-roster/retired recipient, but nothing stops dispatching to the wrong
  still-active recipient or missing a newly-added one; and there is **no bus context-reset
  command** — a wrapped session resets only on an effective-fingerprint change (D-24) or a
  resume self-heal, so `request-restart` preserves the session and a deliberate context clear
  is an operator/host action, not an agenttalk operation.
- **D-26 The PowerShell↔Python file boundary is BOM-free-write + BOM-tolerant-read
  (2026-07-14).** *Why:* the supervisor is generated PowerShell that writes JSON/TOML the
  Python core reads back (and vice-versa). Under **Windows PowerShell 5.1**, `Set-Content
  -Encoding utf8` emits a UTF-8 BOM (EF BB BF); Python's strict `json.loads`/`read_text("utf-8")`
  *rejects* a leading BOM, and `str.strip()` does not remove U+FEFF (skewing a TOML section
  scan). A 2026-07-14 incident-audit found a live case: the codex-home `config.toml` seeded
  BOM-only under 5.1 produced duplicate `[projects]` tables → invalid TOML the external codex
  CLI refuses → the agent can't start. *Decision:* every JSON/TOML artifact the generated PS
  writes uses a **BOM-free** UTF-8 write (`[System.IO.File]::WriteAllText(path, text,
  UTF8Encoding($false))`, matching `Write-StateFileAtomic`), and every reader of a
  **operator-authored or PowerShell-written** config uses **BOM-tolerant** decoding (`utf-8-sig`
  / `ConvertFrom-Json`) — the supervisor state/snapshot files, the codex `config.toml`, the
  operator `settings.json`/`hooks.json`, and the hand-authored `domains.json` / `signoffs.json`.
  (agenttalk's own bus/store artifacts are written atomically BOM-free, so their strict `utf-8`
  reads have no BOM source and are safe as-is — they are not blanket-converted.) The system also
  **self-repairs** an already-corrupted config: the launch-time seed (`supervise
  --seed-codex-config`) runs a SEMANTIC, project-scoped collapse
  (`codex_config.repair_duplicate_project_tables`) that matches single-/double-/bare-/
  case-variant key spellings of the SAME normalized path (so an operator's `[projects."x"]`
  and agenttalk's canonical `[projects.'x']` collapse) and touches only the seeded project's
  table; `codex_config`'s `enable`/`disable` collapse the same way; and both `codex-config
  --status` and `doctor` report a duplicated (invalid-TOML) config instead of presenting it as
  healthy — so a user bitten by the old behavior is healed on the next launch/seed rather than
  left with un-parseable TOML the codex CLI refuses. **D-29 supersedes only the generated-script
  write exception:** generated `.ps1` and `.cmd` artifacts are now BOM-free because their host is
  PowerShell Core 7+ and Windows PowerShell 5.1 is refused before work. The historical incident
  evidence and every `utf-8-sig` tolerant reader remain valid defense in depth for legacy,
  operator-authored, and PowerShell-written files. *Ceiling:* this is a robustness invariant;
  new PS writes and operator-config readers must still follow the BOM-free-write /
  BOM-tolerant-read rule.
- **D-27 Knowledge retrieval is mixed by default, freshness is subject-scoped, and
  curation is causal (2026-07-15).** *Why:* the pointer-only default made accepted
  lessons invisible to normal pull/search/onboard consumers, and a hash of the whole
  domain registry hard-staled every note after any unrelated registry edit. A copied
  curate row could also inherit mutable or forged fields without naming its cause.
  *Decision:* one pure selector consumes one resolved ledger snapshot, evaluates
  pointers and lessons with their existing kind-specific freshness rules, and only
  then searches, orders, and caps separate sections. Mixed JSON is the versioned
  `knowledge-view-v1` envelope; explicit `--type` shapes remain compatible and
  `--output-schema legacy` is the pointer-only escape hatch. New events carry a
  SHA-256 of the normalized effective-domain definition; a subject change is hard
  stale, an unrelated global change is caution, and legacy unscoped rows remain
  visible with caution. The lesson-only virtual `process` policy has a fixed subject
  hash and yields to a real registry entry. New curate/retract rows carry
  `curates_id` and a hash of an exhaustive event-field partition. Bound content
  includes publisher author and creation time, inherited supersession lineage, and
  the note body;
  lesson content includes every persisted field except status/curator, while pointer
  content includes the complete normalized anchor. All remaining event fields are
  explicitly classified as curation/action attestations. Exhaustiveness and
  disjointness guarantee that every field is covered; exact-set tests pin both
  buckets so each field's semantic classification remains an explicit review choice.
  The fold accepts curation
  only when it names the current prior same-key event and its bound content matches.
  Verification metadata such as `verified_against_sha` is deliberately outside that
  content identity. A modern lesson's nested curator must agree with its top-level
  curation actor. Modern rows reject unknown event/authority/lesson/anchor fields;
  historical rows are canonicalized to the same allowlists before folding or output.
  Curate holds the shared config/knowledge lock, re-reads the registry before append,
  and re-stamps both hashes. That A/B check covers supported writers that honor the
  lock. An out-of-band hand edit can bypass it; the per-event subject hash then fails
  closed by making the appended event hard-stale on its first read. Publish aggregates
  independent field errors before registry, Git, or ledger I/O and refuses changing a
  live key's type. *Compatibility ceiling:* legacy curation has no causal id or subject
  hash, so it is accepted only when its content matches the prior current event and its
  registry freshness is necessarily advisory until re-verification.
- **D-28 Coordination stalls require an explicit wait edge and one liveness
  authority (2026-07-15).** *Why:* generic wrapper idleness and concurrent
  mailbox waits are normal, while a wrapped consult can return to its cursor-owning
  loop and leave no durable evidence that it depends on a specific peer.
  *Decision:* manual scoped waits and wrapped `--await-reply` markers normalize to
  one pure wait edge. Wrapped markers are body-free, token-keyed, atomically written,
  and accepted only while their generation matches the live wrapper marker, the
  waiter is freshly `idle_waiting`, and the HMAC-valid (when enabled) outbound thread
  remains open. Supervisor availability is one pure projection of the existing
  heartbeat, health, launch-grace, skew, and planner rules; two matching supervisor
  polls confirm unavailable state. A separate generation-bound observation reports
  a manual restart that remains behind a launch barrier. The detector has no
  kill/restart/release/message/cursor/gate effects. One `coordination_stall` advisory
  source feeds attention, doctor, status, and the dashboard, with stable identity
  independent of age. Consult and handoff opt in; other skills remain unchanged.
  *Deferred:* global all-idle and wait-cycle detection need a cross-subsystem progress
  contract and are not inferred in this release. A launch-barrier observation guards
  supported supervisor polls; an out-of-band state edit is outside that guarantee and
  degrades to unknown or fails closed through the existing marker checks.
- **D-29 PowerShell Core 7+ is the edition baseline, selected once and revalidated
  at every Windows supervisor boundary (2026-07-15).** This decision supersedes the
  rejected pre-build proposal to require 7.4 as a hard floor. The hard gate is exactly
  `PSEdition == Core && Major >= 7`: stable 7.0-7.3 is accepted with an end-of-life
  warning, every prerelease warns, and stable 7.4+ is recommended and quiet. Windows
  PowerShell 5.1 and Core 6 are refused. One stdlib-only authority owns the policy,
  profile-free structured probe, Program Files discovery, native file identity, and
  atomic `.agenttalk/powershell-host.json` record. An explicit `--pwsh` or a generated
  helper's kernel-observed current host is a terminal one-candidate mode; neither falls
  through. PATH and Scheduled Task action strings are diagnostics/data only and are
  never auto-executed or probed.

  A generated supervisor claim holds locks in lifecycle -> selection -> config order,
  retains a query handle to the locator PID, independently checks its image, creation
  time, native identity, activity, and direct or `agenttalk.cmd -> cmd.exe` ancestry to
  the Python CLI, then rechecks the current selection before publishing the singleton
  marker. This prevents accidental wrong-host claims under the existing trusted
  same-user model; it is not authentication, signer/ACL attestation, or proof of mapped
  image/DLL bytes, so the in-process `#requires` and edition guard remain mandatory.
  Selection revisions and fingerprints invalidate the watchdog's process cache; every
  CIM/start-time use rereads TTL and native identity under the selection lock and fails
  open to no snapshot/no kill on ambiguity.

  All three generated `.ps1` files and `bin/agenttalk.cmd` carry one deterministic
  schema/generation marker derived from the fully rendered four-file bundle. Covered
  entry points compare both marker and exact marker-stripped content; partial per-file
  replacement is loud and rerunnable, but is deliberately not group-atomic.
  `--refresh-scripts` and `--init --force` preserve an existing `supervisor.json`
  byte-for-byte and leave runtime state untouched while excluding every *claimed*
  supervisor through the lifecycle lock. *Deferred:* a same-selected-Core process that
  already parsed old script bytes but has not claimed is still a narrow launcher-mutex
  race; atomic task rebind/multi-binding migration and executable signer/ACL attestation
  are also outside this release.

  **2026-07-31 authority correction:** selected-host/image ancestry is not proof
  that `supervisor.ps1` made a runtime observation. A passing test on the held
  #114 branch explicitly authorizes an arbitrary console harness, and a caller
  can also invoke the generated shim manually with the public PID/start
  locators. PR 107 is held pending the design-first authorization change in
  [`DESIGN-supervisor-observation-authorization.md`](DESIGN-supervisor-observation-authorization.md).
  That design treats direct and console ancestry as insufficient, binds the
  live host to the canonical `-File supervisor.ps1` launch plus generated-shim
  shape and artifact generation, and preserves the same-user trust ceiling.

## 6. How we work (process)

- **Per-phase cadence:** architect designs → lead gates the design → builder
  implements in an **isolated git worktree** → cross-review (≥2 distinct
  reviewers) → lead gate → fast-forward merge → release.
- **Lead gate (the bar to merge/ship):** in an isolated worktree off the candidate
  SHA — `ruff`, `bandit -r src -x src/agenttalk/skills`, `git diff --check`, and
  full `pytest` on **Python 3.10 and 3.14**.
- **Release ritual:** bump `src/agenttalk/__init__.py` + `pyproject.toml`; add a
  `CHANGELOG.md` section; update `docs/DESIGN.md` with the subsystem/ADR change
  (or record an explicit no-architecture-change note in the release evidence);
  **append a `docs/ASSURANCE.md` ledger entry attesting the release is
  GOOD/ROBUST/SECURE with its evidence (reviewer verdicts on the final SHA,
  local lead-gate result on 3.10+3.14, CI status across 3.10-3.13,
  adversarial-pass outcome, any new
  known-limitation)**; update every documented install pin and baseline, and
  regenerate the shipped new-user PDF; then commit via
  `git commit -F` (never `-m` for multi-line — PowerShell native-arg trap).
  Before tagging, an operator explicitly dispatches `release-provenance.yml`
  from `master` with that exact 40-character post-bump SHA and version. The
  read-only workflow refuses a moving/different SHA or partial rerun, repeats
  the full gate and CodeQL at the named commit, and retains the exact canonical
  wheel/sdist bytes beside all raw gate evidence and their digests. Increment 1
  creates no tag or release and has no content/package/release mutation
  permission (CodeQL retains its scoped `security-events: write`); after a human inspects
  that evidence, tagging, pushing, and `gh release create` remain manual.
  This repository has one human operator, so the later deliberate action is a
  **temporal double-check**, not separation of duties or two-party control. A
  username allowlist in workflow YAML would not create an authority boundary.
  A release that cannot truthfully carry its assurance entry and exact-SHA
  provenance does not ship.
- **We dogfood the assurance arc on ourselves:** our own work is reviewed and
  gated by the same gate/close/evidence machinery this tool provides.

## 7. Where things live (module map)

| Concern | Module(s) |
|---|---|
| Bus, mailbox, cursors, config, locking, validation, authority | `store.py` |
| PowerShell Core host policy, probing, selection, lifecycle claims | `powershell_host.py`, `supervisor_lifecycle.py` |
| Threads / who-owes-whom | `threads.py` |
| Terminal display / transcripts | `display.py`, `transcript.py` |
| Atomic writes | `_atomic.py` |
| Assurance gate / typed evidence | `gates.py` |
| Assurance scan evidence producer (standalone, no GO/HOLD verdict) | `assurance.py` |
| Milestone close / sign-offs | `close.py` |
| Ephemeral reviewers | `ephemeral.py` |
| Domain registry | `domains.py` |
| Lanes (deliver-gate) | `lanes.py` |
| Knowledge (durable memory) | `knowledge.py` |
| Operator attention queue + dispositions | `attention.py` |
| Dashboard control-plane intent schema + executor | `intents.py` |
| Local Team Console / dashboard server | `web.py`, `web_static/console.js`, `web_static/console.css` |
| Unattended supervisor | `supervisor.py` |
| Wrapper loop and CLI process adapter | `wrapper/loop.py`, `wrapper/run.py`, `wrapper/recv_api.py` |
| Wrapper engine, events, prompts, health, liveness watchdogs | `wrapper/framework.py`, `wrapper/events.py`, `wrapper/prompt.py`, `wrapper/health.py`, `wrapper/work_heartbeat.py`, `wrapper/turn_watchdog.py` |
| Wrapper CLI adapters and sessions | `wrapper/claude_adapter.py`, `wrapper/codex_adapter.py`, `wrapper/session.py`, `wrapper/degraded.py` |
| Managed lead-loop timing and cadence | `lead_loop_runtime.py`, `lead_loop_cadence.py` |
| Dead-letter poison quarantine | `store.py`, `wrapper/loop.py`, `cli.py` |
| Advisory health snapshots | `health.py` |
| Mail-age SLO alarm | `deadman.py` |
| Signing | `signing.py` |
| Capacity hints | `capacity.py` |
| Display avatars / reserved operator principal | `avatars.py` |
| Diagnostics | `doctor.py` |
| Skill installation and bundled-skill currency | `install_skills.py`, `skill_currency.py` |
| Codex sandbox callback setup | `codex_config.py` |
| CLI verbs | `cli.py` |

For the current state of work and known gaps, see **`docs/ISSUES.md`**.
