# Changelog

All notable changes to agenttalk are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.28.1] - 2026-06-22

Makes the v0.28.0 supervisor actually work end-to-end for an UNATTENDED agent
on Windows, plus a roster-safety fix. Hardened across six live kill-and-recover
tests on a real Windows box (each surfaced a distinct real-world failure); the
final cycle — launch with no UAC, reach the listen loop, stay healthy, get
killed, and auto-relaunch with context — passes.

### Fixed
- **Supervisor liveness now uses a UTC clock.** The generated `supervisor.ps1`
  derived "now" from `Get-Date -UFormat %s`, which on Windows PowerShell 5.1 is
  a LOCAL-time epoch. Heartbeats are stamped UTC, so on any non-UTC machine the
  supervisor read every heartbeat as stale by the timezone offset and
  false-killed healthy agents. Now uses
  `[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()`.
- **Launch layer reworked** so a supervised agent can actually start: launch the
  real CLI executable (never a `.cmd`/npm/PowerShell shim that hands off and
  exits), PATH-independent bus calls via a generated shim, Windows argument
  quoting, and an ASCII-only `supervisor.ps1` written UTF-8-with-BOM so PS 5.1
  parses it.
- **Unattended auto-mode.** The supervisor seeds each agent to launch in a
  never-prompt, never-elevate mode (no human to approve a prompt/UAC): Codex
  gets a seeded isolated `CODEX_HOME` (`approval_policy=never`,
  `[windows] sandbox=unelevated` — avoids the per-home admin/UAC install,
  workspace-write), Claude gets `--permission-mode bypassPermissions` + a seeded
  `.claude/settings.json`. A preflight smoke-test fails closed on a bad config
  instead of relaunch-storming.
- **In-sandbox bus access.** Listen/lead skills invoke `python -m agenttalk`
  (bare `python` + `src` on `PYTHONPATH`) so the bus works inside the Codex
  workspace sandbox, where the bare `agenttalk` app-execution-alias is denied.
- **Atomic writes survive the sandbox.** `_atomic.write_text` keeps the atomic
  temp+rename everywhere it works, but on a persistent Windows `os.replace`
  PermissionError (the Codex sandbox holds a write handle for the process
  lifetime) falls back to a direct final-path write. Covers every bus write a
  sandboxed agent makes (publish, cursor advance, heartbeat, waiting).
- **Heartbeat-staleness liveness.** Replaced fragile process-tree/PID "brain"
  discovery (which mis-identified Codex's forking launcher and false-killed
  healthy agents) with heartbeat freshness as the liveness authority — fresh
  means healthy; stale (with the activity hook on) means restart. Fails safe:
  it can miss a dead agent (a no-op) but never kills a healthy one.

### Added
- **`roster add --unique`.** Refuses (exit 3) when the name is an active
  identity and suggests a free variant, so a joining agent never silently
  re-binds someone else's identity. Plain `roster add` stays idempotent (warns
  on an active re-bind). Listen/lead skills pick a unique name on self-join.

## [0.28.0] - 2026-06-17

Two reliability features for long-running multi-agent teams: an explicit
"stand down" signal so a listener is never stopped by accident, and an
external supervisor that auto-recovers agents through server-side outages.

### Added
- **`release` signal + `agenttalk release`.** A dedicated control kind that
  tells a listener to exit its loop — distinct from `end` (which also exports
  a transcript). It's the ONLY thing besides `end` that stops a listener;
  every other message means "work done for now, keep listening". `agenttalk
  release --from A --to B` targets one agent; `--to-group`/`--all` stand down a
  team. A listener obeys `release` only from an `operator_facing`/lead sender
  (fail-closed on ambiguous leadership); release from anyone else is reported
  and the listener keeps listening. No transcript is exported.
- **`agenttalk supervise` — external agent supervisor.** `supervise --init`
  generates a per-team `supervisor.json` + a PowerShell supervisor script that
  launches each agent and monitors it. It **auto-restarts** an agent when the
  process it launched **dies**, and — when the activity hook is installed —
  when the agent **hangs** on an API error (alive but not progressing). Every
  relaunch **resumes the agent's session with full context** (Claude
  `--session-id`/`--resume`; Codex `resume --last`). Per-agent exponential
  backoff (so a real outage doesn't hot-loop), and the human-facing lead is
  protected (warn-only, never auto-killed). The Python core stays thin and
  stdlib-only; the generated script owns terminal launch/kill.
- **`agenttalk request-restart --for <agent>`.** Lead/operator-triggered
  restart: writes a marker the supervisor consumes (kill-if-alive + resume).
  `--force-protected` is required to restart a protected agent.
- **`agenttalk heartbeat` + activity hook.** A throttled liveness stamp the
  supervisor reads. Installed as a PostToolUse hook (`supervise
  --install-activity-hook`, Claude `.claude/settings.json` or Codex
  `.codex/hooks.json`, merge-safe), it keeps the heartbeat fresh *at tool
  boundaries* while working (plus the wait-loop heartbeat while idle), so a
  stale heartbeat reliably means "stuck" — distinguishing a hung agent from a
  busy one. Stuck-recovery (the only path that auto-kills an alive agent)
  fires ONLY when the activity hook is enabled; otherwise a stale heartbeat is
  warn-only, never a kill.

### Notes
- The supervisor replaces the manual "tell each agent who it is + start the
  listen loop" step: the launch command is non-interactive (identity via
  `AGENTTALK_SELF`, listen skill auto-invoked). Launch commands are
  operator-filled in `supervisor.json` (examples provided).
- v1 ships the PowerShell supervisor; a POSIX bash supervisor is a follow-up
  (the Python core is already cross-platform). Codex resume is `resume --last`
  (best-effort) until a real Codex session id is captured.

## [0.27.0] - 2026-06-15

Performance: eliminate the multi-day, machine-wide slowdown caused by
`agenttalk wait` polling. The per-poll cost was `O(waiters × store-size ×
poll-rate)` — every poll re-read and re-validated the entire `messages/`
store, the store never shrank, and dead waiters never stopped polling. Four
changes attack each factor; combined they keep an idle multi-agent bus cheap
indefinitely. All changes preserve existing delivery, validation, signing,
cursor/thread, epoch, and rescind semantics.

### Fixed
- **`wait` no longer re-parses the whole store every poll.** The poll now
  reads incrementally from the agent's cursor — only message files newer than
  the last-seen id are opened/parsed/validated. Per-poll cost drops from
  `O(store)` to `~O(new messages)` (≈18× faster at 800 messages, and flat as
  the store grows). The `since_id` floor is delivery-only; `valid_messages()`,
  epoch/thread/rescind derivation, and the invalid-file report still scan the
  full log.

### Added
- **Adaptive poll backoff.** An idle `wait` backs its poll interval off
  (doubling up to a cap) and snaps back to the base interval the instant any
  activity (message, composing, rescind) appears. Every sleep is clamped to the
  deadline and heartbeat interval, so timeouts and liveness are unaffected.
  Configurable via `--max-poll-interval` (default 2.0s; set at/below
  `--interval` to disable). Cuts idle polling ~6× (≈200→32 polls/min/waiter).
- **`agenttalk compact`.** Bounds live-store growth via conservative prefix
  compaction: archives only a contiguous prefix of delivery-valid messages
  below a safe `keep_floor` (the minimum of every active cursor, the current
  epoch barrier, the earliest message of any non-closed thread group, and a
  keep-tail of recent history). Never archives invalid/tamper files (they stay
  visible to `status`/`doctor`/`prune`). Manual command is always available
  (`--dry-run`, `--keep-count`, `--keep-age-days`, `--json`); an opportunistic
  auto-trigger at wait-arm is **off by default** (`compact.enabled`), threshold-
  and throttle-gated, never a background daemon. Archived messages move to
  `archived/compacted/` (cold storage). Config: `compact.enabled`,
  `compact.keep_count` (1000), `compact.keep_age_days` (30),
  `compact.trigger_threshold` (1200), `compact.min_interval_seconds` (3600).
- **Stale-waiter handling at `wait` arm.** Confirmed-dead `.waiting` markers are
  cleared; a soft-cap warning fires when live waiters exceed a threshold; and an
  opt-in `--refuse-stacked-wait` refuses to arm a second live waiter for the
  same agent (exit **6**). The default remains a non-fatal warning — duplicate
  detection is advisory, not a transport lock.

### Notes
- **Retention boundary.** Cold-archived *closed* history is no longer
  derivable, so `check --to-request` on a historical closed request may return
  `unknown` (exit 4) after compaction — safe and fail-closed, but not
  byte-identical history. Live derivations are unchanged.
- Capacity/headroom snapshots, advisory semantics, and the privacy boundary are
  unchanged.

## [0.26.0] - 2026-06-09

Context-aware coordination phase 1: capacity snapshots now include the
agent's local context-window fill so leads can avoid assigning long,
context-heavy work to agents near compaction.

### Added
- **Context headroom in `agenttalk capacity`.** Snapshots now carry optional
  `context_used_percent`, `context_window_size`, and `context_tokens` fields.
  `capacity show` prints a `context N%` segment and flags near-compaction
  snapshots with `--context-threshold` (default 80).
- **Claude Code context parsing.** Claude refresh reads
  `context_window.used_percentage`, `context_window.context_window_size`, and
  input-side `context_window.current_usage` token counts from the same
  status-line input file used for rate-limit data. A status line with context
  but no `rate_limits` block still publishes.
- **Codex context parsing.** Codex refresh reads
  `payload.info.last_token_usage.input_tokens` and
  `payload.info.model_context_window` from the same rollout `token_count`
  records used for budget data. It uses current input tokens divided by the
  model context window, not cumulative `total_token_usage`. A rollout record
  with context data but no `rate_limits` block still publishes.
- **Lead skill guidance.** Bundled Claude and Codex lead skills now treat
  context headroom as a coarse planning hint alongside rate-limit budget:
  steer long work away from near-compaction agents, ask stale/unknown agents to
  refresh, and warn the operator when every plausible owner is low or near
  compaction.

### Unchanged
- Context headroom is advisory only. Missing, stale, unknown, or
  near-compaction snapshots never block protocol progress, review validity, or
  spec-kitty state transitions.
- The privacy boundary is unchanged: snapshots publish only derived numbers and
  non-secret labels, not raw rollout/status-line contents, prompts, session
  text, provider paths, token bodies, auth paths, or account identifiers.

## [0.25.1] - 2026-06-09

Patch release fixing capacity timestamp parsing on Python 3.10.

### Fixed
- **Codex capacity snapshots on Python 3.10.** `_normalize_ts` fed provider
  timestamps of variable sub-second precision (e.g. Codex's `"...:00.0Z"`)
  straight to `datetime.fromisoformat`, which on Python 3.10 accepts only
  3- or 6-digit fractions and raises `ValueError` — so a real Codex rollout
  yielded no capacity snapshot on 3.10. The fraction is now padded/truncated
  to 6 digits before parsing, so any precision parses on every supported
  Python. Advisory-only: the bug degraded gracefully (no snapshot) and never
  crashed or gated protocol progress.

## [0.25.0] - 2026-06-09

Budget-aware coordination: agents can self-publish a privacy-safe,
advisory snapshot of their local 5-hour and weekly rate-limit budget so
a lead can make better assignment choices during long team runs.

### Added
- **`agenttalk capacity refresh|show`.** `refresh` reads the current
  agent's local Claude Code or Codex budget signal and stores a normalized
  snapshot under `.agenttalk/state/`; `show` (the default) prints the
  team's published 5-hour/weekly usage, reset timing, stale/unknown
  confidence, and near-cap/reset-soon flags.
- **Codex rollout parsing.** Codex refresh reads local
  `~/.codex/sessions/**/rollout-*.jsonl`, prefers the current
  `CODEX_THREAD_ID`, scans for the last `payload.rate_limits` record, maps
  300-minute and 10080-minute windows to 5-hour and weekly slots, and uses
  the rollout record timestamp for staleness.
- **Claude Code status-line parsing.** Claude refresh reads
  `~/.claude/statusline-last-input.json` (or `--statusline-path`) and
  normalizes `rate_limits.five_hour` and `rate_limits.seven_day`.
- **Lead skill guidance.** Bundled Claude and Codex lead skills now tell a
  lead to refresh its own capacity, read team snapshots, steer long work
  away from near-cap agents, and warn the operator when every plausible
  owner is low or unknown.

### Unchanged
- Capacity is strictly advisory. Missing, stale, unknown, or high-usage
  snapshots never block protocol progress, review validity, or spec-kitty
  state transitions.
- Published snapshots contain only derived budget metadata: percentages,
  reset epochs, window lengths, source, confidence, and non-secret plan
  labels. Raw session files, prompts, auth paths, token bodies, account ids,
  and local provider paths stay local.

## [0.24.0] - 2026-06-08

Coordination polish: three multi-agent fixes surfaced by production feedback
(`agenttalk-improvements.md`, items 3.1 / 3.3 / 3.2) and design-reviewed with
the peer agent. All additive and backward-compatible.

### Added
- **Escalation falls back to the team lead.** When `agenttalk escalate` has no
  `operator_facing` liaison configured, it now routes the operator question to
  the team **lead** instead of refusing — so a reviewer needing a human ruling
  is never stranded. Resolution order is `--to` → liaison → the single lead →
  exit 2 (with a remediation naming both `roster set-operator-facing` and
  `roster set-role … lead`).
- **At-most-one-`lead` roster invariant.** `roster set-role <agent> lead` now
  atomically demotes any prior lead and promotes the new one in a single step
  (`demoted X, promoted Y to lead`) — no `--force`, no manual two-step. The
  comparison is case-insensitive; the role is stored verbatim.
- **`doctor` no-escalation-target check.** Warns when a multi-agent team has
  neither a liaison nor a lead, so the gap is visible before an escalation is
  attempted. Absent for solo rosters and when a liaison or lead exists.
- **`wk-` correlation id on `wake` messages.** A `wake` now carries a `wk-` id
  so a reply can echo it, ending the fragile habit of reusing the raw message
  id. An explicit `request_id` is still honored.
- **Owed-inbound pre-send warning.** Before sending a peer unrelated traffic
  while you owe them an open decision-request (a `proposal` or an operator
  escalation), `send` emits a soft, best-effort warning naming the owed id.
  Suppressed when replying on that same `request_id`; silent for non-decision
  traffic; never blocks or fails the send.

### Unchanged (honesty notes)
- The roster `lead` role and the `operator_facing` liaison stay **distinct**
  concepts: the liaison is still the primary escalation target, the lead is only
  the fallback. They are not merged.
- The invariant is **at-most-one** lead, not exactly-one — zero leads remains a
  valid state, and solo / symmetric-pair runs are never forced to have a lead.
- `escalate` still exits 2 when there is genuinely no target; message history,
  message schema, exit codes, and `store.OPENER_KINDS` (wake is not a thread
  opener) are unchanged.

## [0.23.1] - 2026-06-08

Patch release: restore Python 3.10-3.12 compatibility in the Codex config
editor. No behavior change on 3.13+.

### Fixed
- `codex_config` read the per-project config with `Path.read_text(newline="")`,
  but the `newline` keyword on `Path.read_text()` only exists in Python 3.13+.
  On 3.10-3.12 every `enable_project`/`disable_project` call raised
  `TypeError: read_text() got an unexpected keyword argument 'newline'`,
  breaking the feature and turning the CI test matrix red from 0.21.0 onward.
  Reads now go through `open(..., newline="")`, which is portable across all
  supported versions and still disables universal-newline translation so CRLF
  configs are preserved.

## [0.23.0] - 2026-06-08

Dev-discipline skill bundle: agenttalk now ships the five shared coding
discipline skills as version-controlled package content, so teams no longer
depend on untracked personal/global copies.

### Added
- Bundled the devkit skill pack under `src/agenttalk/skills/devkit/`:
  `craft-code`, `test-coverage`, `review-code`, `write-docs`, and
  `review-docs`, including `review-code/references/security.md`.
- `agenttalk install-skills --devkit-only` installs only the devkit pack to
  both `~/.claude/skills/` and `~/.codex/skills/`.
- `agenttalk doctor` now reports `devkit_skills`, surfacing full absence as an
  opt-out-friendly OK hint while warning on partial or stale devkit installs.

### Changed
- `agenttalk install-skills` now installs the devkit by default alongside the
  existing bus skills. Use `--no-devkit` to keep the old bus-only behavior.
- Added `--claude-skills-dir` and `--codex-skills-dir` overrides for testing or
  custom devkit install locations. The existing `--claude-only` and
  `--codex-only` flags continue to scope only the bus-skill side.

### Tests
- Added installer coverage for devkit package presence, default library
  behavior, both-agent install layout, `--devkit-only`, `--no-devkit`, and
  devkit frontmatter.
- Added doctor coverage for absent, in-sync, partial, and stale devkit states.

## [0.22.0] - 2026-06-08

Review-fixes batch 3: low-risk runtime hardening, diagnostic accuracy, and
agent-facing docs/skills contract cleanup.

### Fixed
- Atomic writes are now crash-durable: temp files are fsynced before
  `os.replace`, POSIX parent directories are fsynced after the rename, and
  Windows replaces retry through transient sharing violations from concurrent
  readers. Transcript export now uses the shared atomic writer.
- `/api/state` now degrades to errors-as-data for any single-root collection
  failure, so one corrupt root cannot 500 the whole aggregate.
- Message delivery and thread derivation now fail closed on an empty or corrupt
  roster, delivering nothing instead of falling through to the old empty-roster
  fail-open path.
- `send -m ""` is treated as a deliberate empty body without stdin fallthrough,
  and `send --kind rescind` / `send --kind end` are rejected in favor of the
  dedicated commands.
- Broadcast not-applicable rollup is last-write-wins per member: a later
  substantive reply clears an earlier accidental `--na`.
- Dashboard loopback checks are now address-aware, rejecting non-loopback IPv6
  addresses that merely start with `::1` while accepting IPv4-mapped loopback.
  The server also binds a loopback literal and uses a per-server address family
  instead of mutating `ThreadingHTTPServer` globally.
- `doctor` now warns when a configured operator liaison has never listened, and
  the active-waiters diagnostic is explicit that PID reuse makes it advisory.

### Changed
- Proposal skills now point countered proposals at `meta.in_reply_to` only;
  the removed `meta.counter_request_id` field was never part of the CLI
  contract.
- Handoff skills now mint review-request ids with an `rq-` prefix, keeping
  them visually distinct from proposal ids (`pp-...`).
- Spec-kitty loop skills now use `AGENTTALK_MODEL_TAG` with conservative
  defaults instead of stale hard-coded model-version labels.
- The Codex consult skill now parses `agenttalk status --json` through stdin
  and no longer depends on `bc` for freshness checks.
- Codex listen review guidance now explicitly sends findings back to the
  implementer instead of rewriting a peer's patch during read-only review.
- `SECURITY.md` now states the unsigned-mode trust boundary directly and
  records non-wall-clock cursor / future-id handling as a future design item.

### Tests
- Added `tests/test_atomic.py` coverage for durable atomic writes, cleanup after
  write failure, Windows replace retry, and transcript atomicity.
- Added focused regressions for empty/corrupt-roster fail-closed delivery,
  `send -m ""`, `send --kind rescind/end` guards, broadcast `--na`
  last-write-wins, address-aware loopback checks, localhost literal binding,
  per-server address family, liaison-never-listened warnings, subject/meta HTML
  escaping, and unicode/CRLF multiline body round-trip.

## [0.21.0] - 2026-06-08

Review-fixes batch 2: medium-severity correctness and test-coverage fixes from
the v0.19.0 fresh-review pass. Implemented test-first and Codex-reviewed.

### Fixed
- `/api/state` now applies the same composing-marker freshness rule as the
  CLI: only `0 <= age <= COMPOSING_INTENT_STALE_SECONDS` is shown. Stale or
  future-dated composing markers no longer make the dashboard show abandoned
  writers as actively composing.
- Roster/admin config mutations are serialized with an `O_EXCL` sidecar lock
  around `config.json` read-modify-write operations. This prevents concurrent
  roster changes from silently losing updates, including retire/rename
  tombstones.
- Pairwise open-outbound thread hints no longer name a retired peer as
  `next_owner`. The thread remains visible, but the derived owner no longer
  points at a tombstone that cannot reply.
- `agenttalk codex-config --enable/--disable` now preserves CRLF line endings
  without double-translation, avoiding silent rewrites of Windows-style config
  files.

### Tests
- Added concurrent send coverage for in-process threads and cross-process
  writers to guard against message id/file clobbering.
- Added config-lock tests for live-holder timeout, dead-pid stale-lock cleanup,
  and concurrent roster updates without lost config writes.
- Added focused coverage for stale dashboard composing markers, retired-peer
  thread hints, and CRLF preservation in `codex-config`.

## [0.20.0] - 2026-06-08

Review-fixes batch 1: high-severity findings from the v0.19.0 fresh-review
pass, plus the independently converged HMAC key-health finding. Implemented
test-first and Codex-reviewed.

### Fixed
- Message files whose filename stem does not match the embedded `id` are now
  invalid/quarantinable instead of deliverable. This closes the
  low-sorting-filename / high-embedded-id cursor-poisoning vector.
- Validated message delivery and thread replay now explicitly sort by message
  id, restoring the documented chronological contract; duplicate ids are
  defensively de-duplicated.
- `agenttalk init` now honors the global root contract: `--path`/`--here`
  still wins, then global `--root`, then `$AGENTTALK_ROOT`, then CWD. This
  prevents accidental second-store creation when a root was pinned globally.
- HMAC key loading, inspection, and verification now reject empty, short, or
  garbage key files. `doctor` reports invalid keys as errors instead of
  falsely showing signing as enabled and healthy.

### Notes
- A correctly named future-dated message id can still expose the broader
  clock-skew/monotonic-cursor tradeoff in unsigned mode. That remains a
  separate design item; no ad hoc timestamp quarantine was added in this
  release.

## [0.19.0] - 2026-06-08

Dashboard polish release (issue #22): presentation upgrade on the existing
read-only `serve`/`dashboard` surface. Bus-native only, additive,
read-only — no new routes, no CSP change, no spec-kitty dependency. Design
Codex-accepted; per-WP reviewed.

### Added
- `/api/state` (additive; `schema_version` stays 1): per-agent `sent` and
  `received` message counts; per-root `edges` — an array of
  `{from, to, count}` directed traffic pairs (self-excluded, broadcast
  fan-out included, sorted by count desc, capped to the top 50 with
  additive `edges_truncated`/`edge_limit` when more pairs exist). Computed
  from the validated-message scan `/api/state` already performs (no extra
  walk).
- `/dashboard`: a **hierarchical team layout** — operator-facing liaison /
  lead on top, developer-ish roles left, reviewer-ish right (classified
  client-side from `role`/`operator_facing`); per-agent **cards** (name,
  role/groups, last-seen, sent, received, owes, composing); a
  **who-talks-to-whom** conversation panel from `edges`; a manual
  **Refresh** button and an **auto-refresh toggle** (on by default), wired
  with `addEventListener` (no inline handlers).

### Unchanged
- Routes, per-route CSP (script-capable only on `/dashboard`), exit codes,
  read-only guarantee (no-mutation regression still passes), loopback-only
  posture. Every prior `/api/state` key is intact; degraded roots keep the
  errors-as-data shape (no partial stats/edges).

### Notes
- Stats are bus-native only: the dashboard never imports spec-kitty and
  does not show token usage (not tracked by the bus — a deliberate honesty
  choice, like task-completion counts).

## [0.18.0] - 2026-06-07

Review-hardening release (issue #21): fixes from two independent
fresh-context full-codebase reviews — all cross-feature / cross-release
interactions the per-diff review loop had missed — plus one
operator-requested guardrail. Design Codex-accepted; per-WP reviewed.

### Fixed
- **BLOCKER:** a message file with a non-string `meta.signature` (signing
  enforced) made `hmac.compare_digest` raise an uncaught `TypeError` that
  crashed every read path — including `list_invalid_messages`, so the poison
  file could not even be quarantined. It is now treated as a normal invalid
  (quarantinable) message.
- A message whose `id` is not a generated-id shape (e.g. `"zzzz"`) is now
  rejected as invalid at scan time — previously it delivered and, once acked,
  poisoned the recipient's cursor so every later message was hidden.
- Retired identities' history no longer vanishes from `agenttalk tail` and the
  dashboard message routes (`/api/messages`, `/messages/<id>`, index) — both
  now validate against the known roster (active ∪ retired), matching
  `valid_messages` and the dashboard thread panel.
- `broadcast --resume` no longer gets permanently stuck (exit 5 forever) when a
  frozen recipient was retired after a partial fan-out: retired recipients are
  reported under `dropped` and skipped; an all-retired remainder resolves to
  exit 0. Exit 5 is now emitted only on a genuine active-copy failure.
- Broadcast obligation derivation no longer lists a retired audience member as
  an owed `await-reply` — across `threads`, `sync`, `status`, `whoami`, and the
  dashboard.

### Added
- `agenttalk wait`: advisory warning when another **live** process is already
  waiting as the same agent in the same store (one window per agent is the
  assumed model). Best-effort, never blocks, never changes the exit code.
- `agenttalk doctor`: reports the current `.waiting` marker PID + liveness per
  agent (advisory; not a complete duplicate check).
- Additive fields: broadcast threads carry `audience_retired` (retired members
  of the frozen audience); `broadcast --resume` manifests carry `dropped`.

### Docs / honesty
- README + SECURITY: one window per agent is unsupported-if-violated (warned,
  not enforced); synced multi-machine stores assume clock agreement (id-shape
  validation does not fix clock skew).

### Notes
- The dashboard/`tail` roster-parity fix is a visible change: retired
  identities' historical messages now render where they previously did not.
- Backward-compatible: new fields are additive (absent when unused); old
  on-disk marker/heartbeat formats still read.

## [0.17.0] - 2026-06-07

Obligation dashboard release (issue #20): the bus gets a glanceable,
multi-project, read-only web view of who is doing what — built by
extending the existing `agenttalk serve` server, not forking it.
Design converged over the bus itself (Claude proposal → Codex counter →
accepted) and was Codex-reviewed pre-code and per-WP.

### Added
- `agenttalk dashboard` — alias to the same loopback-only server as
  `serve`, landing on the new obligation view. Repeatable
  `--store <project-root>` watches several projects in one tab (each
  path IS the root — no upward walk; missing stores warn and render as
  degraded panels). Deliberately has **no `--host` option**.
- `GET /api/state` — purpose-built versioned aggregate
  (`schema_version: 1`): roots as an array of namespaced objects with
  roster/roles/liaison, retired tombstones, per-agent presence
  (heartbeat age, unread, composing — an array, matching the
  multi-entry marker), open-thread rows with `next_owner`/`next_action`,
  `mission`/`wp_id` from opener meta, `epoch_at_send` forwarded in its
  exact three-state form plus a derived `epoch_status`
  (`current` / `previous-epoch` / `unknown-pre-epoch`, the
  `check --epoch` vocabulary), broadcast manifests, counts, the current
  epoch, and spec-kitty link metadata when `kitty-specs/` exists
  (filesystem detection only). **No message bodies anywhere.**
- `GET /dashboard` + `GET /static/dashboard.js` — hierarchy HTML
  (liaison first), ~2 s auto-refresh via a self-hosted polling script;
  DOM built via `textContent` only; detail links only for the first
  root (existing `/messages/<id>` routes bind to root[0]).
- Per-root error isolation: a corrupt or uninitialized root degrades to
  an `errors` entry in the payload — never a 5xx, and it recovers on
  the next poll without a server restart.
- `/` (message log) gains a link to `/dashboard`.

### Changed
- `serve`/`dashboard` bind failures now exit **2** with an actionable
  message naming the host:port and suggesting `--port 0` (previously a
  raw `OSError` escaped to the generic handler — a real operator hit
  this as bare `[WinError 10013]` when an unrelated app held 8765).
- `make_server` accepts an additive keyword-only `extra=` list of root
  descriptors; the legacy single-store call shape is unchanged.

### Security
- Per-route CSP split: only `/dashboard` allows (self-hosted) script +
  same-origin fetch; the hostile-body routes keep the pre-0.17.0
  policy byte-identical — both literals pinned by test.
- Read-only proven by regression: a full-tree content-hash walk over
  two stores asserts byte-identical state after mixed dashboard
  traffic (hashes, not mtimes — Windows).
- Loopback wall unchanged: no auth, no remote-bind flag on any
  spelling; multi-root blast-radius framing documented in SECURITY.md.

### Performance
- `/api/state` costs ONE message-dir scan per root per request
  (naively stacking the store's surfaces measured 5+ scans and ~9.6 s
  at 1k messages; derivation itself profiles at ~5 ms).

## [0.16.0] - 2026-06-05

Trusted-team safety release: Phase A of the identity/authz RFC
(`docs/rfc-identity-authz.md`). Identity becomes a first-class registry
with permanent tombstones, rename/retire stop rewriting history, a
lightweight global-epoch barrier plus `check --epoch` give the team a
clean "void the previous run" boundary, and open threads expose who owes
the next move. **This is trusted-team safety, not authorization** — it
assumes a cooperative roster and does NOT defend against a local peer
that forges sends, edits `config.json`, or deletes messages (see
SECURITY.md; later RFC phases B/C/D add real authz and replay hardening).

### Added

- **Identity registry + retirement.** A `retired` list in `config.json`
  records permanent, non-rebindable tombstones. `agenttalk roster
  retire <name>` retires an agent (it can no longer send; its name can
  never be re-bound; its history stays valid). `roster rename <old>
  <new> [--drain-check]` is retirement-not-rewrite: it tombstones `<old>`
  (`renamed_to=<new>`), activates `<new>`, and carries over role / group
  / operator-facing; `--drain-check` refuses while work is owed to/from
  `<old>`. `roster remove` now refuses by default with a retire hint;
  `--force` removes anyway and warns that historical readability breaks
  (no tombstone — the name stays re-addable). Optional `roster forward
  <retired> --to <live> --to-request <rid>` redirects a single owed
  request, transcript-visible (`meta.forwarded_from` /
  `forwarded_request_id`), single hop only.
- **Global epoch barriers.** `agenttalk barrier bump --from <agent>
  --scope global -m "<reason>"` fires one ordinary meta-marked message
  (no new kind) whose message id becomes the global epoch. Any active
  member may bump (a deliberate trusted-team global-stall lever).
  Tracked openers automatically record `epoch_at_send` (three-state:
  absent on pre-0.16 openers, `null` when no barrier has fired yet, the
  barrier id once one has).
- **`check --epoch`.** Extends the pre-action gate with the epoch
  dimension: exit 0 current, exit 3 when the request predates the latest
  barrier (previous-epoch, or a pre-epoch opener that must be re-asked).
  Fails closed on the exit code so automation gating on it is safe.
  Fails OPEN against barrier *suppression* (a deleted barrier) — trusted
  team only, documented.
- **Tool-visible next move.** `threads --json` / `sync --json` open
  threads now carry read-only `next_owner` / `next_action`
  (`reply` / `read-reply` / `await-reply` / `answer-operator`), a pure
  projection of thread state — never settable, never affecting delivery
  or closure. Terminal threads omit them.
- **Doctor.** A new `identity_registry` check reports active/retired
  counts and flags a dangling rename lineage.

### Notes

- Strictly additive over 0.15.0: a store with no retirements, no
  barriers, and no epoch-aware openers behaves exactly as 0.15.0. New
  message-meta keys are ignored by old readers. The one deliberate
  absent-vs-null exception is `epoch_at_send` (see SECURITY.md).
- Broadcast openers snapshot the epoch once before fan-out, so every
  copy of one `broadcast_id` shares a single `epoch_at_send`.

## [0.15.0] - 2026-06-05

Team-scope release: the remaining friction cluster from the four-agent
production band. Reviewer-only questions stop obligating non-reviewers
(and the placeholder-ack workaround dies honestly), broadcast fan-out
stops failing silently partway, and the 562-INVALID-files class of
store debris becomes recoverable quarantine.

### Added

- **Role-scoped audiences.** `agenttalk broadcast --to-role <role>`
  targets every roster member holding a role (sibling resolver — no
  role/group fallback ambiguity; unknown/empty roles refuse loudly
  naming the known set). Every fan-out copy now freezes its audience
  facts at send time (`audience_kind`, `audience_resolved`,
  `batch_total`, `audience_role` for role targets): later roster
  changes never alter historical obligations.
- **Not-applicable replies.** `agenttalk reply --na` closes your
  obligation on a question thread with a structured n/a response,
  displayed distinctly (`na=[...]` / `(n/a)`) for both perspectives.
  Refused on review-request/proposal threads (typed responses
  required); mutually exclusive with --kind; default body without
  stdin sniffing.
- **Broadcast delivery accounting.** A mid-batch failure prints a
  `delivered=[...]` / `missed=[...]` manifest (human and `--json`) and
  exits **5** (new documented code). `agenttalk broadcast --resume
  <bid>` re-sends the missing copies from the frozen originals
  (broadcaster-only, no overrides, accountable itself). `status` warns
  `incomplete fan-out` naming the missed members until resumed or
  rescinded.
- **Quarantine.** `agenttalk prune --invalid [--dry-run] [--json]`
  moves validation-failing files to `.agenttalk/quarantine/` —
  move-only, collision-suffixed, recoverable; selection is the exact
  INVALID gate walk, path-paired at scan time so valid files are
  unselectable by construction. `status` gains a `quarantined` count;
  `doctor` gains a store-hygiene check (inspect-first remediation).

### Changed

- `threads`/`sync` display n/a responders and frozen batch facts
  (additive keys: `responded_na`, `na_response`, `batch_total`,
  `audience_kind`). Bundled skills (both CLIs) teach: reply --na
  instead of placeholder acks; prefer --to-role; recover exit-5
  batches with --resume or rescind; prune with --dry-run first.

### Security

- No trust-model change. Quarantine selection is path-paired (an
  embedded-id collision cannot move a valid file — regression-tested);
  frozen audience/batch meta is untrusted display data (obligations
  derive from the validated copies); `--na` is an ordinary validated
  reply. See SECURITY.md.

## [0.14.0] - 2026-06-05

Operator-safety release, from the four-agent production band's second
retro (2026-06-05) plus a direct operator requirement. Closes the two
incidents that survived 0.13.0: a launch HOLD that crossed mid-flight
with its "fire" message (a voided run), and two terminal windows
silently talking to two different stores. Adds the operator-liaison
workflow: one designated agent the human talks to, with a loud,
correlated escalation path for everyone else.

### Added

- **Rescind.** `agenttalk rescind --from A --to-request RID [--to-id MSG]
  [-m reason]` marks a tracked request you opened as no-longer-current.
  First-class and transcript-visible (in the known kinds, NOT a hidden
  control kind); thread derivation reports `closed-superseded` for every
  participant; the first qualifying rescind decides and later duplicates
  are audit-only; requester-only. A re-ask after a rescind needs a fresh
  request_id. A per-agent manual `ack` keeps its own `closed` label —
  view closure never masks the fact.
- **Pre-action currentness gate.** `agenttalk check --for A --to-request
  RID [--json]` prints `current`/`superseded`/`unknown` and exits 0/3/4.
  The contract for irreversible actions: run it immediately before
  acting on a request you drained earlier — the executor-already-read
  race cannot be closed by any inbox primitive, only by this gate.
  Read-only and ack-independent.
- **Rescinded wake.** A scoped `wait --to-request` on a rescinded
  request wakes immediately (at entry or mid-wait) with a `RESCINDED`
  banner and **exit 3**, instead of blocking for a reply that should
  never come. Exit 1 remains exclusively the wait timeout.
- **Operator liaison.** `agenttalk roster set-operator-facing <name>`
  (single slot — "two liaisons" is unrepresentable; `--clear` removes)
  designates the one agent the human operator talks to directly.
  `agenttalk escalate --from W -m "..."` routes an operator question to
  the liaison automatically as a tracked question (`esc-` request_id,
  printed as `request_id=<id>`), and refuses loudly (exit 2, with
  remediation) when no liaison is resolvable. The liaison's `sync` shows
  pending escalations under OPERATOR INPUT NEEDED; an answer on the same
  request_id clears them. Advisory routing metadata — not authorization.
- **Root hardening.** New `AGENTTALK_ROOT` env var with strict
  precedence (`--root` flag > env > upward walk; a pinned root that has
  no store fails loudly, never falls back). `agenttalk init` refuses to
  create a nested store when one exists up-tree (`--force` for a
  deliberate sandbox). `doctor` detects and names every store from the
  working directory upward, flags split-brain layouts, and both `doctor`
  and `whoami` lead with `root:` as their first line.
- **Liaison diagnostics.** `doctor` checks the operator-facing
  designation: unset-but-escalations-exist, configured-but-pruned
  (error), and stale-heartbeat liaison.
- **Reply-in-flight visibility.** `agenttalk composing --to-request RID`
  binds the ping to one thread (the counterparty is derived from the
  thread — single argument), extends the peer's scoped wait, and records
  an observational marker that `threads`/`sync` show as
  "(reply in flight)" — suppressing the stale-thread warning while a
  reply is being drafted. Allowed exactly when you owe the thread's next
  move (including the needs-info ping-pong).

### Changed

- `threads`/`sync`/`status` surface the new states: `SUPERSEDED` rows
  with rescind provenance (who/when/why), a RESCINDED section in `sync`
  for rescinds the agent has not yet consumed, the liaison's escalation
  bucket, `[operator-facing]` markers, and new warnings (stale pending
  escalations; escalations-with-no-liaison). All JSON additions are
  strictly additive: absent when the features are unused, never null.
- Bundled skills (both CLIs) teach the four new contracts: check before
  irreversible actions; rescind over prose retractions; escalate instead
  of asking your own window's human (with the liaison's single-voice
  rule); and `composing --to-request` while drafting long replies.
- The `composing` help text no longer claims a 240s default wait
  timeout (the actual default is 120s).

### Security

- No trust-model change. `rescind` is validated content (same roster/
  HMAC gates as any message; derivation honors only the requester's
  rescind); `check` answers from the validated log and cannot be masked
  by per-agent state; `operator_facing` is advisory routing metadata in
  config.json, explicitly not an authorization boundary; the
  reply-in-flight marker is observational with the heartbeat/waiting
  tamper profile. See SECURITY.md.

## [0.13.0] - 2026-06-03

Workflow-safety and Windows-ergonomics release for the remaining
production-retro rough edges: safer reply routing, robust body input,
and explicit identity diagnostics before agents act after a restart.

### Added

- **Reply dry-run.** `agenttalk reply --dry-run` resolves the reply
  anchor (`--to-id`, `--to-request`, or last received message), prints
  the would-be recipient, echoed `request_id`, and kind, then exits
  without sending. This makes broadcast/thread-originator routing
  inspectable before a reply is committed.
- **stdin body files.** `--file -` now reads the body from stdin across
  `send`, `reply`, `propose`, and `broadcast`, giving Windows users a
  reliable here-string-friendly path for multi-line text, apostrophes,
  backslashes, and paths.
- **Identity diagnostics.** `agenttalk whoami [--for A] [--json]`
  reports the effective root, resolved self and peer, roster
  membership, role/groups, unread count, and owed-thread count. It
  warns when identity is unset or the resolved agent is not in the
  roster, which usually points to a misplaced `--root` or env typo.

### Changed

- Bundled skills and README now teach the safer restart bootstrap:
  `agenttalk roster` -> `agenttalk status` -> `agenttalk sync --for A`
  before acting after a restart, context compaction, or long idle
  period.
- Invocation docs now emphasize that `--root <path>` is a global
  option and must precede the subcommand, and that env vars set inside
  one LLM tool-call shell may not persist into the next call.
- Windows body guidance now defaults to here-strings piped to
  `--file -`, with machine-readable roots, paths, request ids, and
  routing data carried in `--meta key=value` rather than prose alone.
- Lead/reviewer/liaison guidance now states the authority boundary:
  re-derive HOLD/GO, ownership, and pending-review state from the
  repository, operator, `sync`, `threads`, and spec-kitty when
  applicable, not from stale message-body prose after a restart.

## [0.12.1] - 2026-06-03

### Fixed

- **Scoped wait respects global consumption.** `agenttalk wait --to-request`
  no longer re-delivers a message already consumed through the global cursor
  by `drain` or plain `wait`. Scoped delivery now starts at
  `max(per-thread seen_msg_id, global cursor)`, so after draining and answering
  a needs-info request it awaits the next reply instead of re-showing the old
  one. Scoped wait remains non-consuming and advances only the per-thread
  pointer.

### Documentation

- Clarified the global-cursor vs per-thread-state model: `reply-waiting`
  derives from the global cursor; `threadstate.json` is created lazily by
  `wait --to-request` and `ack --to-request`, not at init; `ack --to-request`
  closure is permanent, and a re-ask needs a fresh `request_id`. SECURITY notes
  threadstate tampering has the same denial-of-service profile as cursor
  tampering. ROADMAP now reflects that 0.12.0 delivered the first three
  production-retro items.

## [0.12.0] - 2026-06-03

Coordination recovery release from the first four-agent production
retro. The goal is to make restarts and busy team inboxes recoverable:
agents can rejoin with a digest, wait on one known thread without
consuming unrelated traffic, and manually close handled threads when
the strict request/response contract was not enough.

### Added

- **Scoped wait.** `agenttalk wait --for A --to-request RID` and
  optional `--kind K` return only matching addressed messages. Scoped
  waits advance only the per-thread `seen_msg_id` pointer and never
  advance the global inbox cursor, so unrelated traffic stays unread.
- **Per-agent threadstate.** `.agenttalk/state/<agent>.threadstate.json`
  stores per-request `seen_msg_id` and `closed` state used by scoped
  waits and manual closure. This is additive and leaves existing
  cursor files backward-compatible.
- **Explicit thread closure.** `agenttalk ack --for A --to-request RID`
  manually marks a handled request thread closed for that agent
  without touching the global cursor.
- **Rejoin digest.** `agenttalk sync --for A [--json]` summarizes
  identity, roster, actionable request threads, terminal decisions,
  recent unread non-action traffic, and deterministic next-action
  hints for restart/context recovery.

### Changed

- **Question closure is broader.** Question-style threads, including
  broadcast questions, close when the expected counterparty sends any
  non-control response with the same `request_id`. Review requests
  still require `review-result`, and proposals still require
  `proposal-response`.
- **Bundled skills now use scoped waits for known threads.** Handoff,
  consult, propose, listen, sk-loop, and lead docs teach
  `wait --to-request`, `sync --for`, and `ack --to-request` so agents
  do not wake on unrelated traffic or assert stale state after a
  restart.
- README documents the new global-cursor vs per-thread-state mental
  model, sync workflow, and 0.12.0 CLI surface.

### Security

- No trust-model change. The new threadstate is local coordination
  metadata inside the existing `.agenttalk/` state directory; message
  validation and optional HMAC behavior are unchanged.

## [0.11.1] — 2026-06-02

Patch release from the fresh-reviewer experiment: Claude Code and
Codex each spawned a fresh sub-agent with no prior context to review
the v0.11.0 multi-agent release, then compared findings.

### Fixed

- **Broadcast thread detection is now stricter.** `agenttalk threads`
  no longer treats any opener carrying free-form `meta.audience` as a
  broadcast. A normal point-to-point `question`, `review-request`, or
  `proposal` with a stray `--meta audience=...` now stays on the
  pairwise path, so its expected response kind can close the thread.
  Broadcast derivation requires `kind=question` plus
  `meta.broadcast_id`, which the `agenttalk broadcast` command always
  sets.
- **Roster mutators tolerate null team maps.** A config containing
  explicit `groups: null` or `roles: null` no longer crashes roster
  admin operations with a `TypeError`. `roster set-role`, `set-group`,
  and `add --role/--group` now coerce null or absent team maps to an
  empty map before mutation.

## [0.11.0] — 2026-06-02

Adds the multi-agent team surface: roster roles/groups, broadcast
fan-out, multi-party thread tracking, and lead skills. This release is
designed around named local participants rather than process
supervision: agenttalk remains the file-backed bus, while humans or
external launchers start worker windows.

### Added

- **Roster roles and groups.** New `agenttalk roster` command shows
  agents, roles, group memberships, and resolved identity. Admin
  subcommands add/remove agents, set roles, and set named groups. The
  implicit `all` group is reserved.
- **Broadcast fan-out.** New `agenttalk broadcast --to-group <group>`
  / `--all` writes one message per recipient, excluding the sender.
  Broadcast `message`/`note` are FYI fan-out; broadcast `question`
  tracks one response obligation per recipient by reusing
  `meta.request_id` with a `b-...` broadcast id.
- **Multi-party thread state.** `agenttalk threads` recognizes
  broadcast questions, showing responded/pending recipients for the
  broadcaster and owed-inbound state for each recipient until they
  reply with `agenttalk reply --to-request <b-id>`.
- **Lead skills.** Added `/agenttalk.lead` for Claude Code and
  `$agenttalk-lead` for Codex. The lead coordinates named agents and
  groups using point-to-point sends, broadcast questions, and thread
  tracking. It never spawns worker processes and does not duplicate
  spec-kitty.

### Changed

- Bundled send/listen/handoff/consult/propose/sk-loop skills now
  generalize from a hardwired `SELF`/`PEER` pair to named targets and
  groups while preserving the `claude`/`codex` defaults.
- Listen skills document broadcast fan-out handling: answer broadcast
  questions back to the sender on the shared request id; do not
  reply-all unless explicitly asked.
- README documents team setup, fresh-review naming, broadcast/groups,
  and the lead role. SECURITY.md documents that roles/groups are
  routing metadata, not authorization boundaries.

### Security

- Team support does not change the trust model: rostered participants
  are still inside the same local trust boundary. Optional HMAC remains
  project-key based and does not provide per-agent crypto identity.

## [0.10.0] — 2026-06-02

Adds three coordination features developed jointly by Claude Code and
Codex over agenttalk itself (collaborative design, a split
implementation, and cross-review in both directions): thread tracking,
first-class proposals, and anchored replies.

### Added

- **Thread tracking.** New `agenttalk threads [--for A] [--all]
  [--json]` command derives request/reply state from validated
  messages only. Default output shows actionable rows
  (`reply-waiting`, `owed-inbound`, `open-outbound`); `--all` includes
  `closed`. `--json` provides stable `threads[]` and `counts` fields
  for skill automation. State is computed by a chronological
  ball-passing replay, so the review `needs-info` round-trip
  (reviewer → requester → reviewer) and the multi-round consult
  convention (a follow-up reusing the original `request_id`) are
  tracked correctly rather than getting stuck.
- **First-class proposals.** New `proposal` and `proposal-response`
  message kinds, plus `agenttalk propose`, support accept/reject/counter
  decision flow. Proposal correlation reuses `meta.request_id` with a
  `pp-` prefix; counters close the old proposal with
  `proposal-response status=countered` and open a fresh proposal with
  `--in-reply-to <old-request-id>`.
- **Anchored replies.** `agenttalk reply --to-id <message_id>` and
  `--to-request <request_id>` anchor a response to a specific received
  message or thread, avoiding accidental correlation to the latest
  unrelated inbox message.
- **Bundled proposal skills.** Added `/agenttalk.propose` for Claude
  Code and `$agenttalk-propose` for Codex.

### Changed

- `review-request`, `question`, and `proposal` now auto-mint
  `meta.request_id` when absent (`rq-`, `q-`, and `pp-` prefixes).
  Missing `request_id` warnings now cover both `review-result` and
  `proposal-response`.
- `agenttalk status` warnings now include unconsumed correlated replies
  and stale outbound threads, derived from the same thread helper as
  `agenttalk threads`.
- Bundled listen, handoff, and sk-loop skills now require
  `agenttalk threads --for <agent>` before declaring work done or going
  idle, document proposal/proposal-response handling, and explicitly
  state that proposals cannot bypass user approval for split work.
- The `send` skills (both sides) now frame `question` as a tracked
  `q-` thread (steering true fire-and-forget pings to
  `message`/`note`) and list the new proposal kinds, pointing at
  `agenttalk propose` as the intended entry point.

### Internal

- New `Store.valid_messages()` exposes the roster- and
  signature-validated message set (all recipients) that thread
  derivation consumes, so a forged or unsigned response can never
  falsely close a real thread. `messages_for` is refactored on top of
  the shared `_validated_messages()` gate.

## [0.9.0] — 2026-05-24

Minor release. Fixes the issue #5 coordination hiccup (jointly
reported by Claude + Codex): a two-agent **soft deadlock** where each
agent waited on the other. Root cause was discoverability — `recv` is
a non-consuming *peek*, but its name implies consumption, so one agent
never advanced its cursor (`unread` climbed to 30) and hand-rolled
fragile timestamp polling instead of using the built-in cursor. This
release makes the consume path obvious, makes the stall visible, and
tightens request↔result correlation.

### Added

- **`agenttalk drain --for <agent>`.** The single obvious "consume my
  inbox" verb: prints all unread AND advances the cursor to the newest
  message. Mechanically identical to `recv --ack` (shares the exact
  code path) and advances past hidden control messages too. Use this
  instead of hand-rolled polling.
- **`recv` peek hint.** Plain `recv` (no `--ack`, no `--since`, not
  `--quiet`) that prints messages now emits a one-line stderr hint that
  the cursor did NOT move and points at `drain` / `recv --ack`.
  Suppressed for `--ack` (consuming), `--since` (deliberate history
  inspection), and `--quiet`.
- **Mutual-wait / soft-deadlock detection.** `agenttalk wait` now
  writes an observational `.waiting` marker (pid, since, cursor,
  timeout, epoch deadline) while blocking and clears it on exit
  (message, timeout, or interrupt). `agenttalk status` flags a
  **soft-deadlock** when two or more agents are blocked in `wait`
  simultaneously, and warns when any agent has **unread but a
  never-set cursor**. The marker is strictly observational — like the
  heartbeat, nothing about delivery or correctness depends on it, and
  orphaned markers from crashed shells are detected as stale via
  heartbeat age + the recorded deadline.
- **Auto-correlation for review-requests.** `agenttalk send --kind
  review-request` (and `reply --kind review-request`) now mints a
  `request_id` into `meta` when one isn't supplied, and prints it.
  `review-result` without a `request_id` emits a soft stderr warning
  (exit code unchanged) so request↔result threading is enforced by
  convention without breaking mixed-version peers.

### Changed

- `agenttalk status --json` gains a top-level `warnings` array and
  per-agent `waiting` / `waiting_stale` fields. All pre-existing keys
  are preserved (additive change — existing consumers keep working).
- README + skill bodies document the cursor / `--ack` / `--since`
  mental model in one place: **`recv` peeks, `drain` (or `recv --ack`)
  consumes, `wait` consumes one real message, `--since` inspects
  history without moving the cursor.**
- Reconciled `pyproject.toml` version (was stale at 0.7.2) with the
  package `__version__`; both are now 0.9.0.

### Tests

- Added coverage for `drain` (consume-to-newest, hidden-control-only
  advance, `--include-control`, `--quiet`), the `recv` hint
  (fires/suppressed matrix), `.waiting` marker lifecycle (written
  mid-wait, cleared on message + timeout), `status` warnings
  (never-acked unread, live soft-deadlock, stale-marker exclusion,
  JSON schema), and request_id autogen + soft missing-result warning
  across both `send` and `reply`.

## [0.8.0] — 2026-05-22

Minor release. Fixes a real-world sharp edge: a peer's reply landed
~12 s after `agenttalk wait` timed out at 240 s, so the reply was
lost to the waiter even though it was already on disk. Adds two
mechanisms — a small post-timeout grace window for clock-jitter
cases, and a new `composing` control kind so a peer drafting a long
reply can keep the waiter's deadline alive on demand.

### Added

- **`composing` message kind.** New control-plane kind that the
  peer can send while drafting a long reply. `agenttalk wait`
  consumes each fresh composing ping as a deadline-extension signal
  (`--composing-extend SECONDS`, default 120 s; hard cap 1800 s
  total per wait) and does NOT surface composings as a returned
  reply. `agenttalk recv` hides composings from its default view
  (`--include-control` opts back in). Old receivers on
  ≤ 0.7.2 silently drop the unknown kind, so the feature degrades
  gracefully across mixed versions.
- **`agenttalk composing --to <peer>` subcommand.** One-line helper
  for the most common composing-ping flow. Same write path as
  `send`: validated, optionally HMAC-signed, logged in the
  transcript and dashboard. Use periodically while drafting:
  ```powershell
  agenttalk composing --from $SELF --to $PEER -m "still drafting"
  ```
- **`agenttalk wait --grace SECONDS` (default 2.0).** Post-timeout
  grace window: when the deadline fires, sleep this long and do
  ONE final inbox scan before exiting 1. Catches replies that
  landed in the last fraction of a second before the deadline.
  `--grace 0` reproduces the pre-0.8.0 hard-edge behavior.
- **`agenttalk wait --composing-extend SECONDS` (default 120.0).**
  Per-composing-ping deadline extension, capped at 1800 s total
  per wait. `--composing-extend 0` disables extension while still
  consuming composings (they don't surface as replies).
- **`agenttalk recv --include-control`.** Surfaces control-plane
  kinds (currently just `composing`) in the inbox view. Useful for
  debugging deadline extensions or auditing what the peer pinged.
- **sk-loop skill update.** Both Claude and Codex sk-loop skills
  gained two patterns:
  - **Cycle-2+ example request:** if the reviewer's feedback is
    ambiguous, the implementer sends a `question` asking for a
    concrete sketch BEFORE guessing into another rejection. Uses
    the existing `question` kind — no new bus surface.
  - **Composing pings during long replies:** the drafting agent
    sends `agenttalk composing` every ~2 min so the peer's wait
    doesn't expire mid-reply.

### Changed

- **`Store.last_received_for(agent)` now skips `CONTROL_KINDS`** by
  default, so `agenttalk reply` auto-correlates to the most recent
  real message rather than to a stale composing ping. The kept-out
  set is configurable via a new keyword argument.
- **`agenttalk send --kind`** help text now lists `composing` as a
  known kind and points to the dedicated `composing` subcommand as
  the preferred entry point.

### Tests

- 10 new tests in `tests/test_cli.py` covering: post-timeout grace
  returns a late message; `--grace 0` preserves pre-0.8.0 behavior;
  composing extends the deadline and is not returned as a reply;
  `--composing-extend 0` disables extension but still consumes the
  ping; duplicate composings counted only once (no runaway loop);
  the `composing` subcommand writes the right kind + body; `recv`
  hides composings by default; `--include-control` surfaces them;
  `--ack` advances past a hidden composing so a stale ping can't
  pin a cursor forever.

## [0.7.2] — 2026-05-22

Patch release. UX cleanup for the upgrade path: surfaces drift
between bundled and installed skill files better and makes
`install-skills --dry-run` actually look like a dry run.

### Fixed

- **`agenttalk install-skills --dry-run` is now visibly
  different from a normal run.** Previously, a target that
  differed from the bundled version reported `skipped` in both
  real and dry-run modes, which made `--dry-run` look broken
  (same wall of `! skipped` lines as a real run). Dry runs now
  emit `would-skip` (target differs, `--force` not set, would
  not write) and `would-overwrite` (target differs, `--force`
  set, would overwrite). Each output line carries a short tail
  explaining the state, e.g.
  `? would-skip … (differs; local edits preserved)`.
- **`agenttalk install-skills --dry-run --force` previews
  `--force`** without writing — the recommended path before
  destroying local edits is now "dry-run --force first, then
  `--force`".

### Changed

- **`agenttalk doctor` skill-drift output is now actionable.**
  The existing `claude_skills` / `codex_skills` warn check now:
  - Names the differing files in the human-readable `details`
    (was just `N/total differ from bundled`), so you don't
    have to re-run `install-skills` to find out which.
  - Leads the `fix` hint with `--dry-run --force` (preview)
    before suggesting the destructive `--force` step. Previously
    jumped straight to `--force`, which destroys local edits
    without warning.
  - Carries a per-check `data` payload in `doctor --json`
    (`{"target": "...", "missing": [...], "differs": [...],
    "total": N}`) so loops and scripts can act on drift without
    re-parsing the human output.

### Added

- Three regression tests:
  - `test_dry_run_reports_would_skip_when_target_differs_no_force`
  - `test_dry_run_with_force_reports_would_overwrite_no_writes`
  - Doctor: extends the existing drift test to pin the new
    file-naming + `--dry-run --force` hint + JSON `data` payload.

## [0.7.1] — 2026-05-21

Patch release. Fixes a chronology bug in message id generation
that surfaced as a flaky test in CI on fast hardware after the
0.7.0 release.

### Fixed

- **`_new_id` is now strictly monotonic within a process.** The
  format ``YYYYMMDD-HHMMSS-uuuuuu-XXXX`` documented
  lexicographic-equals-chronological order, but the random
  4-char suffix did not preserve order when two messages landed
  in the same microsecond. ``messages_for`` and the new web
  dashboard sort by id to display chronological order, so the
  bug could surface two same-microsecond messages in send-
  reversed order (visible in ``recv``, ``tail``, and
  ``/messages``). Fix: per-process monotonic clock — if a new
  call would tie or go backwards relative to the last issued
  id, the timestamp is bumped by 1µs. Cross-process collisions
  (two agents writing the same microsecond) are still handled
  by the random suffix; each process tracks its own last id.

### Added

- ``test_new_id_is_strictly_monotonic_under_load`` (tight-loop
  regression test, 2000 calls) that pins the invariant against
  future fast-hardware regressions.

## [0.7.0] — 2026-05-21

Adds a read-only local web dashboard so the message log can be
browsed in a real browser instead of being limited to terminal
output. No write actions; no new runtime dependencies (stdlib
`http.server` + `html` + `json`).

### Added

- **`agenttalk serve` — read-only local web dashboard.** Starts
  a small HTTP server (default `http://127.0.0.1:8765/`) that
  renders the roster, signing status, message list, and per-
  message detail pages. **Loopback-only by design** — the only
  accepted `--host` values are `127.0.0.1`, `::1`, and
  `localhost`; there is no flag to expose the dashboard
  elsewhere. SSH-tunnel `localhost:<port>` if you need to view
  it from another machine. All HTML output is escaped, a strict
  `Content-Security-Policy` blocks inline JS, and only
  `GET`/`HEAD` are allowed.
- **JSON endpoints for scripting.** `/api/status` mirrors the
  same data `agenttalk status --json` reports plus `hmac_key`
  health and `invalid_messages`. `/api/messages` returns every
  validated message; `/api/messages/<id>` returns one.
- **Tests:** `tests/test_web.py` (20 tests) covering the index
  render, JSON endpoints, message detail, POST/PUT/DELETE
  rejection, path-traversal handling on `/messages/<id>`, HTML
  body escaping (defense against an LLM smuggling JS via a
  message body), refusal to bind any non-loopback host,
  per-request loopback peer check on **every** HTTP method
  (not just GET — closes a method-skip information leak), IPv6
  URL bracketing (`http://[::1]:port/`), response security
  headers, and parity-with-`recv`/`tail` checks: messages with
  unknown `kind`, out-of-roster sender/recipient, or bad HMAC
  signatures do NOT render and instead surface under
  `/api/status.invalid_messages`.

### Security

- The dashboard treats message bodies as untrusted: every body
  is escaped via `html.escape` AND the page sets
  `Content-Security-Policy: default-src 'none'; style-src
  'unsafe-inline'; img-src 'none'; frame-ancestors 'none'` so
  even if an escape were ever missed, inline JS would not run.
- The dashboard reuses `Store.messages_for`'s validation surface
  (schema, roster, kind, and HMAC when `signing_enforced()`).
  Messages that fail any of these checks do not appear in
  `/api/messages` or `/messages/<id>` — they surface under
  `/api/status.invalid_messages`, matching the
  `recv`/`tail`/`doctor` invariant.
- The per-request loopback-peer check runs before EVERY HTTP
  method. Earlier dashboard iterations let POST/PUT/DELETE/PATCH
  skip the check and return 405 without the peer gate, which let
  a non-loopback probe distinguish "server present" from "you
  are blocked." Both now collapse to 403.

## [0.6.0] — 2026-05-21

Security release. Closes the `kind=end` forgery scenario and the
zizmor follow-up from 0.5.1.

### Added

- **Optional HMAC-SHA256 message signatures.** Stdlib-only
  (`hmac` + `hashlib`). Key lives in the per-user config dir
  (`~/.config/agenttalk/keys/<project_id>.key` on POSIX,
  `%LOCALAPPDATA%\agenttalk\keys\<project_id>.key` on Windows) —
  outside `.agenttalk/` so an attacker with project-dir read
  CANNOT also read the key. **Enforcement is anchored to the
  per-user key file's existence at a PATH-DERIVED `project_id`
  (SHA-256 of the absolute project root path)**, NOT to any field
  in attacker-writable `.agenttalk/config.json`. The iter-1
  review caught that a `require_signatures` config flag was
  bypassable by editing config.json; the iter-2 review caught
  that a config-stored `project_id` was bypassable the same way.
  After iter-2, **nothing inside `.agenttalk/` is load-bearing
  for signature enforcement** — both the project identity and the
  policy live outside the project dir. To enable: `agenttalk
  hmac-init`. To disable: delete the key file. Tradeoff: moving
  the project dir changes its path-derived `project_id`, so you
  re-run `hmac-init` at the new location (documented in
  SECURITY.md). Wire format pinned by a golden test
  (`tests/test_signing.py::test_canonical_payload_format_is_stable`)
  and documented in `src/agenttalk/signing.py`. Defaults OFF
  (zero-setup path unchanged from 0.5.x).
- **`agenttalk hmac-init [--force]`** — bootstrap the project's
  signing key. Writes mode 0600 on POSIX. Activates enforcement
  on both send AND verify sides — there is no separate "enable"
  flag.
- **`agenttalk status` / `status --json`** — surfaces `project_id`
  (path-derived), `signing_enforced` (key-file presence), and —
  when enforced — `hmac_key` health (path / exists / readable /
  mode warning / in-project-dir warning). Legacy
  `require_signatures` and `project_id` fields in config.json
  from upgraded 0.6.0-iter-1/2 configs are surfaced with a NOTE
  that they're ignored (kept for diagnosability).
- **`agenttalk doctor`** — new `hmac` check, ok/warn/error with
  remediation hints (no project_id, key file inside project,
  permissive mode, unreadable, etc). Reports `disabled` when no
  key file exists (the OK default).
- **`.github/workflows/*.yml`** — every third-party action now
  hash-pinned (`actions/checkout@<sha>  # v4` etc.); `# vX`
  comment kept dependabot-friendly.
- **`.github/dependabot.yml`** — weekly auto-bumps for GHA SHAs
  and dev-dep security updates.

### Changed

- **zizmor now votes** in CI (was non-voting in 0.5.1). All
  third-party actions are hash-pinned so the `unpinned-uses`
  default policy is satisfied. Workflow-level permissions stay
  read-only; CodeQL grants itself `security-events: write` at
  job level.
- **`Store.project_id()` is path-derived.** SHA-256 of the
  absolute project root path; not stored in config. The iter-2
  review found that a UUID stored in config.json could be
  tampered with to disable enforcement; the iter-3 design moves
  project identity entirely outside `.agenttalk/`. Existing 0.5.x
  configs continue to work unsigned; enabling signatures on any
  project (new or existing) is just `agenttalk hmac-init` — no
  config edits, no flags. Tradeoff: moving the project dir
  changes its `project_id`, so the key file must be regenerated
  at the new location. Documented in SECURITY.md.
- **`agenttalk tail` now validates HMAC** before rendering
  message bodies (iter-1 review caught that tail bypassed
  signature verification and rendered forged bodies as normal
  output). Forged-signature / unsigned-when-required / missing-
  key messages now surface as `TAIL INVALID` warnings on stderr;
  the body is never rendered.
- **`Message.from_dict` vs `Message.from_raw`** distinction kept;
  signature ops live in `agenttalk.signing` so `Message` stays a
  pure data layer per Codex's design feedback.

### Security

This is the first release that actually defends against an
adversarial *other local user with project-dir write access*.
Earlier releases were honest about that being out of scope; 0.6.0
makes it opt-in. **Limits documented in SECURITY.md:** HMAC does
NOT defend against the same OS user (they can read the key
anywhere it's stored), does NOT solve replay / deletion /
reordering / cursor tampering (those need a hash chain anchored
outside `.agenttalk/`, tracked for later), and does NOT validate
timestamps for freshness (clock skew would create avoidable
failure modes).

### Tests

- 258 passing + 2 skipped (POSIX mode-bit tests skipped on
  Windows). Up from 227 in 0.5.2. Coverage includes canonical
  payload golden test, sign/verify round-trip, every failure
  mode (missing/wrong-version/wrong-key/tampered-body/wrong-key-
  id), key file I/O + mode 0600 enforcement, key health
  inspection (missing/secure/in-project-dir/loose-mode), Store
  integration (sign-on-send, verify-on-read, unsigned-rejected-
  when-key-exists, forged-signature-rejected, valid-signature-
  accepted, zero-setup backwards-compat). Two iter-2 regression
  tests are critical: the config-tamper bypass repro (config
  edit cannot disable enforcement) and the tail-bypass repro
  (tail uses the same verifier as messages_for).

### Notes for upgraders

- **Existing projects:** zero change required. Without a key file
  for this project, enforcement is off; old configs work
  unchanged. (Note: 0.5.x's path-relative install instructions
  and 0.6.0's path-derived project_id mean that moving a project
  directory changes its identity. If you upgrade in place and
  don't move the dir, nothing changes for you.)
- **To enable signatures on a fresh project:**
  ```
  agenttalk init --here --agents claude,codex
  agenttalk hmac-init
  ```
  Enforcement activates as soon as the key file exists.
- **To enable on an existing project:** just `agenttalk hmac-init`.
  No config edits, no flags. The project_id is derived from the
  absolute project root path. There is no `require_signatures`
  flag in 0.6.0 — earlier iter-1 designs had one but Codex's
  review found it was attacker-writable and therefore useless;
  the key file's existence at the path-derived ID is the policy
  anchor. (iter-2 review also caught that a config-stored
  project_id had the same bypass; both are now removed.)
- **Skill bodies are unchanged** in this release. Existing
  installs of the bundled slash commands don't need
  `agenttalk install-skills --force`.

## [0.5.2] — 2026-05-21

Tag-hygiene patch. v0.5.1 introduced ruff in CI but the
auto-fixes for six test files (removing unused imports, dropping
an unused variable) landed on disk but weren't staged into the
release commit. CI on the v0.5.1 tag would have failed ruff. This
patch commits those cleanups so the tagged tree matches the
local-clean baseline.

### Fixed

- `tests/conftest.py`, `tests/test_codex_config.py`,
  `tests/test_reply_tail.py`, `tests/test_reset.py`,
  `tests/test_skill_lint.py`, `tests/test_store.py`: dropped
  unused imports (`os`, `json`) and an unused assignment that
  ruff F401/F841 had flagged in 0.5.1 but the v0.5.1 commit
  missed staging.

No behavior changes. 227 tests still pass. Ruff `check src tests`
now reports `All checks passed!` against the *committed* tree
(matching the v0.5.1 promise).

## [0.5.1] — 2026-05-21

Tooling-only patch. No behavior changes; closes the "CI scanner
integration" track from the v0.2.0 review report.

### Added

- **GitHub Actions workflows.**
  - `.github/workflows/security.yml` — runs the full security
    scanner stack on every push to master, PR, and a weekly
    schedule: ruff (with `S` rules), bandit, pip-audit, gitleaks,
    semgrep (registry + custom local rules), CodeQL
    `security-extended`, and zizmor (GHA workflow audit).
  - `.github/workflows/tests.yml` — pytest matrix across Python
    3.10–3.13 on ubuntu/windows/macos.
- **Custom semgrep rules** at `.semgrep/agenttalk.yml` enforcing
  agenttalk-specific invariants:
  - Raw agent names must not be interpolated into state filenames
    without `validate_agent_name()`. (The class of bug fixed in
    0.2.1; semgrep flags it before it ships.)
  - Messages must go through `Store.send()`, not direct file
    writes into `.agenttalk/messages/`. (Bypasses the KNOWN_KINDS
    + atomic-write guarantees.)
  - `exec()` / `eval()` on a message body is a hard fail (the
    explicit prompt-injection threat called out in SECURITY.md).
- **Ruff configuration** in `pyproject.toml`. Line length 120;
  rules `E F B S C4` selected. Per-file ignores for tests
  (asserts + literal "passwords" used as message bodies).
- **New optional dependency group:** `[security]` installs ruff,
  bandit, and pip-audit for local pre-commit-style runs.

### Changed

- Several small style cleanups across `src/agenttalk/` and
  `tests/` to satisfy the new lint baseline. No behavior
  changes — `Message.from_raw` field loop variable renamed
  (`field` shadowed an import), exception chains use
  `raise ... from e`, and a few long lines were wrapped.

### Tests

- Still 227 passing.
- Bandit run produces 0 issues at all severity levels.
- Ruff `check src tests` produces 0 errors.
- pip-audit runs against `pip freeze --exclude-editable` from
  a fresh `pip install -e ".[dev]"` env. First CI run will be
  the ground truth for the dev-dep CVE set.

### Notes

- Scanners run in CI only. There are no new runtime dependencies;
  the agenttalk package remains stdlib-only.
- `SECURITY.md` updated: "CI scanner integration" moved from
  "Still planned" into "Delivered in 0.5.1".

## [0.5.0] — 2026-05-21

Third slice of the v0.2.0-review roadmap. Adds two ergonomic
commands that close the gap between the existing primitives.

### Added

- **`agenttalk reply`** — reply to the most recent received message.
  Auto-derives the recipient (= sender of the last message) and
  auto-echoes `request_id` from the original meta so the peer's
  `wait`-then-match logic works without the agent manually
  threading the correlation token. Explicit `--meta request_id=...`
  wins over auto-echo. Kind defaults to `message` (the safe
  no-op default) rather than auto-promoting to `review-result`.
  Resolves the v0.2.0-review "agenttalk reply" item.
- **`agenttalk tail`** — passive monitor mode. Streams every
  message as it arrives, using the same display renderer as
  `wait`/`recv`, **without** advancing any cursor or writing any
  heartbeat. Safe to run in a third terminal alongside two active
  agents — they don't see tail as a listener. `--from-start`
  replays the entire store first; `--timeout N` exits after N
  seconds (default 0 = run until Ctrl-C). Resolves the v0.2.0-
  review "agenttalk tail/watch" item.
- **`Store.last_received_for(agent) -> Message | None`** — the
  underlying primitive for `reply`. Honors schema/roster
  validation: a tampered message that `messages_for()` would
  filter is never returned as "the last message".

### Tests

- 227 passing (was 208 in 0.4.0). New file `tests/test_reply_tail.py`
  covers: reply auto-derives recipient, auto-echoes request_id,
  explicit meta wins, empty inbox exits 2, kind defaults to
  message, explicit kind wins, env-driven self, unknown kind
  rejected; tail streams only new by default, --from-start
  replays, never advances cursors, never writes heartbeats, exits
  0 on timeout, picks up messages injected during the run, NEVER
  renders forged/unknown-kind message bodies (surfaces them as
  INVALID warnings on stderr), surfaces unparseable-JSON warnings
  too; last_received_for returns most recent, returns None on
  empty, skips invalid messages.

## [0.4.0] — 2026-05-21

Second slice of the v0.2.0-review roadmap. Adds explicit session
lifecycle (`agenttalk reset` / `reset --archive`) and clarifies
what `init --force` actually does.

### Added

- **`agenttalk reset [--archive]`** — clears **active bus state**
  (messages, cursors, heartbeats) and bumps `session_id`.
  **Preserves the config (roster) AND historical transcripts under
  `.agenttalk/sessions/`** — exported transcripts are user-visible
  artifacts, not active bus state. With `--archive`, instead moves
  everything (messages + state + sessions) into
  `.agenttalk/archived/<old_session_id>/` so the entire prior
  session is recoverable. Closes the v0.2.0 review's "no clean
  lifecycle around old messages" gap. Session IDs are now
  validated as safe filesystem-path fragments at config-load time,
  so a corrupt config can't smuggle a `..\\escaped` path through
  to `--archive`.
- **`Store.reset(archive=False) -> (cfg, archive_path | None)`**
  and **`Store._archive_session(session_id) -> Path`** as the
  underlying mechanism. Same-filesystem `shutil.move` so even
  large message dirs archive instantly. Collision-safe: a second
  archive into the same `session_id` writes timestamped sub-dirs
  rather than overwriting.

### Changed

- **`agenttalk init --force` semantics clarified.** Previously
  ambiguous about whether `--force` cleared in-flight messages.
  The help text now spells out: it rewrites `config.json` only;
  state (`messages/`, `state/`, `sessions/`) is preserved. Users
  who want a clean slate are pointed at `agenttalk reset`.
- **`Store._new_session_id()` now includes a 4-char random
  suffix.** The old `YYYYMMDDTHHMMSSZ` format collided when two
  session boundaries fell in the same second (init then reset).
  New format: `YYYYMMDDTHHMMSS-XXXXZ`. Filename-safe; transcript
  paths stay unique.

### Tests

- 208 passing (was 184 in 0.3.0). New file `tests/test_reset.py`
  covers: state clearance, session-id rotation, archive
  preservation (including transcripts under `--archive`),
  transcript preservation by default, double-archive collision
  safety, empty cursor recreation, uninitialized-store error, CLI
  default vs `--archive`, exit-2 on uninitialized, the explicit
  `init --force does NOT clear` guarantee, the `init --help` text
  mentions `agenttalk reset`, the session-id traversal regression
  (corrupt config rejected at load time), parametrized rejection
  of 7 unsafe session-id forms, and parametrized acceptance of
  both old (`YYYYMMDDTHHMMSSZ`) and new (`YYYYMMDDTHHMMSS-XXXXZ`)
  formats for backwards-compat.

## [0.3.0] — 2026-05-21

First slice of the v0.2.0-review feature wave. Adds two new
commands, product-level message-schema hardening, structured-output
support for automation, and a substantial test-coverage backfill.

### Added

- **`agenttalk status --json`** — structured output for the consult
  freshness check and any external automation. Schema:
  `{root, session_id, message_count, invalid_messages[], agents[],
  stale_threshold_seconds}`; each agent entry exposes `cursor`,
  `unread`, `heartbeat` (ISO 8601 or null), `last_seen_seconds`,
  and `stale` (bool or null).
- **`agenttalk doctor`** — single-command health check. Reports:
  is the store initialized, are bundled skills installed and in
  sync with the bundled package, is the per-project Codex sandbox
  block configured, and is each agent's heartbeat fresh. Each check
  carries `ok / warn / error` plus a one-line remediation hint.
  Supports `--json` for the same payload. Per the global exit-code
  contract, exit 2 on any error; warnings exit 0 with the warning
  state visible in output. (Exit 1 stays reserved for `agenttalk
  wait` timeout.)
- **Message-schema validation on read.** New `KNOWN_KINDS`
  vocabulary; `Message.from_raw(data)` strictly validates raw JSON
  before constructing a Message (catches missing/wrong-type id, ts,
  from, to, kind, subject, body, meta — including the `meta=[]`
  truthy-coercion bug and non-dict roots); `Message.validate(roster)`
  enforces known kind + roster membership. `Store.messages_for()`
  silently skips invalid messages so a forged file with an unknown
  kind cannot smuggle a fresh instruction surface into the listener.
  `Store.list_invalid_messages()` returns every parse failure
  alongside every schema/roster failure so tampering and disk
  corruption are visible rather than silently swallowed; `agenttalk
  status` shows the count and points users at `status --json` for
  per-message details.
- **Send-time kind validation.** `Store.send()` rejects unknown
  kinds at write time — without this, a `--kind typo` would exit 0
  with the message silently undeliverable on the receive side.
- **Skill-body untrusted-input guidance.** listen, sk-loop, and
  consult skill bodies (both sides) now carry explicit prompt-
  injection-resistance rules: message bodies are data the LLM is
  asked to read, not instructions the LLM is asked to follow;
  state transitions must derive from validated metadata + repo
  reading, never from body prose alone.
- **Consult skill uses `status --json`** for the heartbeat
  freshness check instead of parsing the human-formatted output —
  the JSON contract is the stable one.

### Tests

- Suite grew from 93 to 184. New coverage: `status --json` schema,
  `doctor` for every check category (uninitialized / fresh / stale
  / missing heartbeat / out-of-sync skills / missing codex-config
  block), message-schema validation (each rejection reason
  individually + the read-time silent-skip), `transcript.py`
  markdown + jsonl + unicode + export round-trip, `display.py`
  rendering with kind/subject/meta/empty-body/multiline/unicode
  cases, `cmd_end` (sends `kind=end` + writes transcript),
  `cmd_transcript` (md + jsonl), `cmd_wait` (already-queued
  message, timeout, heartbeat write, --ack vs --no-ack cursor
  behavior), `agenttalk --version`. Also a new skill-body lint
  asserting every required policy substring appears in both the
  Claude- and Codex-side skill bodies, catching cross-side drift.

### Changed

- `Store.messages_for()` now silently skips messages that fail
  schema/roster validation. `Store.all_messages()` returns only
  messages that survived strict raw-JSON construction; parse
  failures and construction failures are surfaced separately via
  `Store.list_invalid_messages()` (consumed by `agenttalk status`
  and `agenttalk status --json`).
- README + SECURITY.md updated to mention the new commands and
  the schema-validation behavior. README's `--kind` documentation
  also updated: kinds are now a fixed vocabulary (KNOWN_KINDS), not
  free-form; adding a new kind requires updating that constant.

### Security

This is a *data-integrity* hardening pass, not a cryptographic
one. Schema validation catches malformed or out-of-vocabulary
messages, but cannot defend against an attacker who writes
well-formed messages — that requires signing and is tracked for
0.4.0+ as opt-in HMAC (see `SECURITY.md`). The skill-body
prompt-injection guidance is the procedural counterpart and
applies regardless of whether crypto is later added.

## [0.2.1] — 2026-05-21

Patch release driven by the v0.2.0 cross-agent code review (see
[`docs/reviews/v0.2.0.md`](docs/reviews/v0.2.0.md)). Closes one
security blocker plus three quality issues that landed in the same
review pass.

### Security

- **Agent names are now validated against a safe-identifier
  pattern** (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`). Before, names
  were interpolated directly into filesystem paths like
  `.agenttalk/state/<name>.cursor`, so a name containing `..`,
  `/`, or `\` could escape `.agenttalk/state/` and write
  arbitrary files inside the project root. `agenttalk init` and
  every identity-resolution path (`--from`/`--to`/`--for` flags,
  env vars, on-disk config) now reject unsafe names with exit 2.
  Duplicate names in the roster are also rejected.

### Fixed

- **`--meta bad_value_without_equals` now exits 2** instead of 1.
  The old `SystemExit(str)` form exited 1, which collides with
  `agenttalk wait`'s timeout signal and could confuse loop skills.
- **`agenttalk codex-config` writes are now atomic.** The
  user-global `~/.codex/config.toml` was being written with a
  plain `Path.write_text()` — a crash mid-write could truncate
  a file that controls every Codex project. Now uses the same
  temp-file + `os.replace` helper as the message store.
- **`agenttalk codex-config` now handles project paths containing
  apostrophes.** A path like `D:\Code\Bob's Repo` previously
  emitted invalid TOML (`[projects.'...Bob's Repo']`). The
  section-key renderer now uses a TOML basic string with proper
  escaping when the path contains a single quote.

### Changed

- `agenttalk._atomic.write_text` factored out of `store.py` as a
  shared helper; `codex_config.py` now uses it.
- `Store.load_config()` validates the on-disk roster on read, so a
  malformed `config.json` (wrong types, unsafe names) errors out
  with a clear message instead of cryptic downstream tracebacks.
- README: agent-identity section now documents the safe-identifier
  rule; CLI reference gained an Exit Codes table.

### Tests

- New tests cover: the safe-identifier pattern (parametrized over
  valid + invalid names, including trailing-newline cases that the
  Python `$` anchor would have missed), exact- and case-insensitive
  duplicate-roster rejection, load-time corrupt-config detection,
  the path-traversal regression (Codex's review repro), invalid-
  `--meta` exit code, apostrophe-path TOML emission, idempotent
  round-trip through the new TOML quote helper, and the
  codex_config atomic-write contract across all three call sites.
- Total suite: 93 tests, all passing.

### Docs

- `SECURITY.md` added: trust model, threat scenarios with expected
  behavior under the current model, planned 0.3.x product-level
  hardening, recommended CI scanner stack, and a clear statement of
  what `agenttalk` does NOT defend against (same-OS-user attacker,
  shared filesystems, prompt injection in message bodies).
  Synthesizes the cross-agent security consult done via
  `/agenttalk.consult` on the same day.
- `docs/reviews/v0.2.0.md` added: the v0.2.0 review report that
  drove this patch.

## [0.2.0] — 2026-05-21

First tagged release. Builds out the message bus into a full
cross-agent collaboration surface, with bundled slash commands for
Claude Code and Codex, a persistent spec-kitty loop, and a
pre-answer consult primitive.

### Added

- **`agenttalk codex-config`** — manages per-project sandbox/trust
  block in `~/.codex/config.toml` so Codex can call agenttalk from
  inside its sandbox. Idempotent enable/disable/status with TOML
  parsing that handles single- and double-quoted Windows-path keys
  and inline-comment table headers.
- **`agenttalk install-skills`** — copies bundled skill files to
  `~/.claude/commands/` and `~/.codex/skills/`. Idempotent,
  preserves user edits unless `--force`; supports `--dry-run`,
  `--claude-only`, `--codex-only`.
- **Bundled skill set** (5 per side, installed by
  `agenttalk install-skills`):
  - `/agenttalk.send` — fire-and-forget message.
  - `/agenttalk.listen` — passive listener with reentrant wait,
    mode-detecting `review-request` handling (spec-kitty vs ad-hoc
    cross-review), and consult-request critique routing.
  - `/agenttalk.handoff` — bundled send + wait with structured body
    template and `request_id` correlation.
  - `/agenttalk.sk-loop` — persistent spec-kitty implement/review
    loop driven by `spec-kitty next` state machine, with `kind=wake`
    signals between two persistent CLI windows. Role-symmetric.
  - `/agenttalk.consult` — confer with the peer *before* answering
    the user. Sends a structured draft + uncertainty, waits for
    critique, synthesizes a concise final answer naming agreement
    and disagreement.
  - Codex side mirrors all five under `~/.codex/skills/agenttalk-*`.
- **Heartbeats** — `agenttalk wait` stamps
  `.agenttalk/state/<agent>.heartbeat` every 10 s (configurable via
  `--heartbeat-interval`); `agenttalk status` shows `last_seen=<age>`
  per agent so peers can see whether the other side is actively
  listening. Pure observability, no LLM tokens.
- **`agenttalk --version`** — prints the installed version (useful
  for support).
- **Pytest suite** — 46 tests across store, codex_config,
  install_skills, and cli, including regressions for every
  Codex-caught bug from earlier review rounds.

### Changed

- **Env-driven agent identity.** `--from`/`--to`/`--for` are now
  optional on every CLI command and fall back to `$AGENTTALK_SELF`
  / `$AGENTTALK_PEER`. In a 2-agent roster, the peer can be derived
  automatically. Required to support running two agents of the same
  kind (e.g., two Claudes) with distinct names.
- **Roster validation.** Resolved identities must be in the project
  roster — typos exit 2 with a clear message instead of silently
  operating on a phantom mailbox. Self-mail (`--from X --to X`) is
  rejected.
- **Listen wait timeout 30 s → 1800 s.** Pure listen mode has
  nothing else to interleave with; short timeouts were burning tokens
  on idle wake-ups. sk-loop keeps its 30 s for the spec-kitty poll
  interleave.
- **sk-loop made role-symmetric.** Both `implement` and `review`
  actions are equally normal for either agent; spec-kitty's state
  machine assigns roles per WP.
- **No-auto-split rule** baked into listen, handoff, and send skill
  bodies: outside spec-kitty, agents must not coordinate a split of
  implementation work without first asking the user. When the user
  approves a split, every implemented piece must be cross-reviewed.
- **`agenttalk init` output** now prints concrete env-setup commands
  for both terminals when the roster has exactly two agents.

### Fixed

- Heartbeat parser previously returned a naive datetime for
  timezone-less heartbeat files, crashing `status` via
  aware-vs-naive subtraction. Now returns `None` on naive input —
  status degrades to `(no heartbeat)` instead of crashing.
- `codex-config` parser previously failed to match TOML basic-string
  keys with escaped backslashes (e.g.
  `[projects."d:\\Projects\\repo"]`) and headers with inline comments
  (e.g. `[projects.'...'] # comment`), silently creating duplicate
  blocks instead of being idempotent.
- `cli.main()` exit-code propagation: `python -m agenttalk` now
  exits with the correct status code (was always 0).
- Windows stdout/stderr reconfigured to UTF-8 in `cli.main()` —
  arrow and em-dash characters in message bodies no longer crash via
  the cp1252 codec.
- README install path was ambiguous (`pip install -e .\agenttalk`
  required the wrong CWD). Now standardized on `cd agenttalk &&
  pip install -e .`.
- `OSError` / `PermissionError` is now caught in the top-level
  exception handler — permission failures give a clean CLI error
  instead of a traceback.

### Notes

- Package remains stdlib-only (no third-party runtime dependencies).
- Python 3.10+ required.
- Not on PyPI by design. Install via tag-pinned git URL (see README).

## [0.1.0] — initial commit

- Core file-backed message bus (`agenttalk init`/`send`/`recv`/
  `wait`/`ack`/`transcript`/`end`/`status`).
- Atomic JSON-per-message writes; per-agent cursors; markdown +
  JSONL transcript export.

[0.12.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.12.0
[0.11.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.11.1
[0.11.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.11.0
[0.10.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.10.0
[0.9.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.9.0
[0.8.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.8.0
[0.7.2]: https://github.com/zoolok17/agenttalk/releases/tag/v0.7.2
[0.7.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.7.1
[0.7.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.7.0
[0.6.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.6.0
[0.5.2]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.2
[0.5.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.1
[0.5.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.0
[0.4.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.4.0
[0.3.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.3.0
[0.2.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.1
[0.2.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.0
