# agenttalk — issues & work tracker

The living record of **what we are doing and why**: work in flight, planned
fast-follows, accepted known limitations, and the backlog. Pairs with
`docs/DESIGN.md` (the why behind the architecture) and `CHANGELOG.md` (what
shipped). Keep this current — when an item ships, move it to "Recently shipped"
with its version; when a new finding lands, add it with a disposition.

**Conventions.** Status: `IN PROGRESS` · `PLANNED` · `KNOWN LIMITATION`
(shipped & accepted) · `BACKLOG` · `SHIPPED`. Severity: `P0` critical · `P1`
major · `P2` minor · `P3` nit. Each item: what, why, where, disposition.

---

## Recently shipped

- **SHIPPED · v0.60.1 / `d6bab84` — cross-path operator-answer dedup.** Closed the
  v0.60.0 known-limitation: both the browser drain and the CLI relay now route the
  final operator-answer send through one `Store.send_operator_answer_atomic` that
  re-checks pending + sends under the non-reentrant `_config_lock`, so a cross-path
  race loser sees `answered` and is denied with zero sends (fail-closed on
  lock/read/mismatch/reject; two-phase crash-recovery preserved). Reviewed: both bus
  reviewers approve + a fresh adversarial concurrency pass (8-way + mixed race + 30
  stress iterations, all single-send).
- **SHIPPED · v0.60.0 / `6827801` — operator inbox: answer escalations from the
  browser.** New `answer_escalation` intent kind through the write spine; shared
  `resolve_operator_answer_target` enforces pending + needs_operator + owed-to-actor
  + anti-self + not-coalesced as distinct fail-closed predicates; two-phase drain
  (reconcile-before-live-check) so a crash-retry never double-answers; server-injected
  operator meta; `/api/attention` answerable annotations gated on `--enable-actions`
  (actions-off byte-identical); plain-language inbox + answer composer. `relay
  operator-answer` now also refuses a coalesced wrapper twin (`superseded_by_canonical`).
  Reviewed: adversarial design gate (PASS_WITH_CONDITIONS, 8 folded) + 4-way final-SHA
  review (2 bus reviewers approve, 2 fresh adversarial passes could-not-break) + a
  coalescing-fold re-review (both approve).
- **SHIPPED · v0.59.3 / `75c818f` — Team Console per-provider capacity + compact
  density.** Failure-isolated synchronous capacity refresh in the supervised loop;
  supervised Codex reads only its isolated `CODEX_HOME/sessions` (bounded, fail-closed
  to unknown, never a stale value as observed); additive `/api/state` capacity fields;
  real CSS-variable compact density; four fail-closed web/store P3s.
- **SHIPPED · v0.59.2 / `1dd10ba` — supervisor process-ownership attribution.**
  Closed the cross-project-kill P0 with typed process ownership, strict live-chain
  descendant proof, and launch-nonce confirmation so a supervisor reaps only this
  project's own agent tree.
- **SHIPPED · v0.59.1 / `9d88dc1` — Team Console write-spine hardening.**
  Added drain-time frozen-plan revalidation, pid-start-aware anti-reuse reclaim,
  torn-intent quarantine, deny-on-drift behavior, and the negative/e2e security
  regression suite.

## SHIPPED · v0.59.2 — supervisor cross-project process kill (2026-07-04)

**P0 · a supervisor in project B can KILL an identically-named agent's processes in an
unrelated project A on the same machine.** Operator hit this live: a standalone `claude` in
project A was killed when the orbit-launcher project started *its* lead + supervisor.

ROOT CAUSE: the supervisor takes a MACHINE-WIDE process snapshot (`supervisor.py:1837`,
`Get-CimInstance Win32_Process`) and identifies "its" agent processes by AGENT NAME in the
command line, with NO project-root scoping. `_wait_row_for` (`supervisor.py:389-397`) matches
any `agenttalk … wait --for <agent>` process ANYWHERE on the machine; that row is added to the
managed set (`_launcher_managed_set`, `supervisor.py:560-562`) → `_targets()`
(`supervisor.py:975-977`) → `Stop-Tree`/`Stop-Process -Id` (`supervisor.py:2143`, `2247`). The
one root-scoped signal (parent-process ancestry to the recorded `launcher_pid`) is correct; the
name-based command-line match is the leak. The DEFAULT names `claude`/`codex` collide across
projects by construction, so any two default-named projects on one host can cross-kill. TRIGGER:
project B's `kill_first` cleanup on launch/restart of its same-named agent.

MITIGATION (no code, effective now): use UNIQUE agent names per project — the `--for <name>`
match then cannot collide across projects.

FIX (P0, design→review — NOT a panic patch): root-scope EVERY process match — a process counts
as this project's agent only if its command line carries this project's `--root <path>` (and
supervised agents must launch with an explicit `--root`), not merely a matching `--for <name>`.
A careless fix could stop the supervisor reaping its OWN agents (false negatives → no
auto-recovery), so it needs the design pass. Also audit `_discover_brain` strategy-1 (climbs
from the cross-matched wait row) and the brain name-pattern match (every `claude.exe` matches)
for the same leak.

STATUS: shipped in `v0.59.2` / `1dd10ba`.

**P0 UPDATE — investigation complete (workflow `wrnuxcwka`, 2026-07-03).**

VERDICT: **CONFIRMED — code mechanism (laptop-independent).** [CORRECTION 2026-07-03: the
agenttalk team and the orbit-launcher team run on SEPARATE laptops; the orbit checkout on THIS
(agenttalk) laptop is a STALE COPY, so the live incident state — orbit's supervisor + a
same-named `claude-dev` on ONE machine — is on the orbit laptop and NOT inspectable from here.
The operator's account STANDS; the "refuted on-disk" note below was a wrong-laptop visibility
artifact. The bug is in `supervisor.py` (identical on both laptops), so the fix applies
unchanged.] Original workflow verdict: The CODE MECHANISM (name-collision, no project-root scoping)
is FULLY CONFIRMED and mechanically demonstrable in current master. The **specific** "orbit
killed a claude-dev" pairing is **REFUTED** against current on-disk state: orbit-launcher has NO
`supervisor.json` and NO `claude-dev` (its agents are codex-lead/nova/vega/…); a host-wide grep
found NO project with a literal `claude-dev` agent; the only `supervisor.json` present is
agenttalk's own. So the exact actors named in the report are not on disk now (rosters shifted
since the incident, which was on **v0.56.0**), and the literal event can't be re-created — but
the bug CLASS is live: `FEEDBACK-perf-slowdown-2026-06-15.md` captured real orbit agents running
bare `agenttalk wait --for <name>` loops with **no `--root`** (claude-developer-2, claude-vega,
codex-orbit-dev), the exact command-line shape the matcher keys on. Honest limit: we confirmed
the class, not the literal incident.

CONFIRMED CHAIN (file:line): machine-wide snapshot (`Get-CimInstance Win32_Process`,
supervisor.py:1837) → `_wait_row_for` (389-397) matches `agenttalk … wait --for <agent>`
ANYWHERE on the host (name-only, no root/cwd/ancestry) → `_launcher_managed_set` adds it
UNCONDITIONALLY (560-562, OUTSIDE the launcher_pid ancestry gate at 552-558) → `_targets`
copies it verbatim (975-977) → STUCK_RECOVER (1130-1136, `kill_first`) or MANUAL_RESTART
(1009-1014) → `Stop-Tree` → `Stop-Process -Id -Force` (1868). The start-time guard (1867) is
PID-reuse-only and MATCHES a genuinely-live foreign process, so it does NOT block the cross-kill.
Max-likelihood trigger: a never-launched agent (first poll → `launcher_pid=None` → ancestry
block skipped → the foreign wait row is the SOLE managed member) whose per-project heartbeat is
stale. ENABLER: supervised launch argv carries no `--root` (wrapped args supervisor.py:1647) and
the planner never threads `store.root` into the matcher — nothing distinguishes project A's
`--for X` from project B's.

FIX SPEC (design basis for the normal-cadence build): (1) emit `--root <abs>` onto every
supervised launch argv (supervisor.py:1647) so the command line is self-identifying (env
AGENTTALK_ROOT is not visible to the Win32_Process query — the cmdline token is the carrier);
(2) thread `store.root` through plan_actions → `_liveness` → `_launcher_managed_set` / `_wait_row_for`
(the planner is already invoked with `--root $Root`, supervisor.py:2112); (3) a name-matched wait
row is eligible as a KILL target ONLY if its command line also carries this supervisor's
normalized root token — else DROP it (fail-closed; may stay a diagnostic-only `wait_alive`
signal); (4) keep the `launcher_pid`-ancestry match (552-558, already root-unique) as the PRIMARY
reaper so NO false negative is introduced, and so the empty/access-limited-cmdline degrade case
still reaps via ancestry; (5) update `agenttalk.listen.md` to ALWAYS pass `--root <abs>` (else a
manual agent that omits it stops being reaped — a regression); (6) close the same leak in
`_ephemeral_kill_targets` (1163-1180, also calls `_launcher_managed_set`).

REGRESSION TEST: two-root snapshot — project A `wait --for claude-dev --root C:/projA` (pid P_A,
ancestry to launcher_A) + project B same name `--root C:/projB` (P_B, ancestry to launcher_B);
run plan_actions with root=C:/projB, launcher_pid=launcher_B, heartbeat stale. ASSERT (a) P_A NOT
in kill_targets (cross-kill closed); (b) B's own tree IS reaped on stuck_recover (own-reaping
preserved); (c) an EMPTY-cmdline row with ancestry to launcher_B is STILL reaped (degrade-to-
ancestry). Mirror it through `supervise --plan --root` for the CLI wiring.

KEY RESIDUALS to fold: `_discover_brain` strategy-1 (484-496) is seeded by the same cross-matched
wait row and can pin a FOREIGN brain (mis-pin/false-liveness) — root-scope or demote it too;
a stale/foreign recorded `launcher_pid` is a separate cross-kill vector the wait-gate doesn't
cover (state hygiene); normalized-path comparison must handle PS quoting / mixed slashes /
drive-case / trailing slash (too-loose re-opens the bypass, too-strict re-breaks own-reaping).

STATUS: shipped in `v0.59.2` / `1dd10ba`. Historical pre-fix mitigation was
project-DISTINCT agent names across concurrent projects.

**IMPLEMENTATION UPDATE - process ownership model (2026-07-04).**

The fix is broader than root-scoping a name match. The supervisor planner now produces typed
ownership records first, then emits a closed kill target set from those records. A row is killable
only when it is a nonce-confirmed recorded launcher, a direct same-root owning wrapper, a direct
same-root owning wait row, the bounded same-agent brain/TUI reached from that wait row, a strict
live-chain descendant from a seed, launch-time child provenance, first-confirmed child provenance,
an exact versioned prior, or a freshly re-derived legacy row. Generic same-root CLI rows, same-root
rows for another agent, foreign-root rows, shell/terminal hosts, and ambiguous rows are
diagnostic/branch-cut only.

A recorded launcher is not confirmed by pid/start alone. Supported supervised `python -m agenttalk`
and `agenttalk` console-script wrapper launches get a hidden top-level
`--supervisor-launch-nonce` marker in the command line. Launcher confirmation requires the live row
to be branch-clean for the current root/agent, have a readable command line, parse as this
`wrap --for <agent> --loop`, and carry the exact current nonce before the subcommand. Unsupported
native launches and pre-upgrade launchers without a nonce are intentionally suppressed as
`confirmed_launcher` until a supported relaunch writes the marker. That can miss cleanup for the
launcher itself, but it is fail-safe: it does not authorize a generic pid/start-colliding process.

Operator-visible diagnostics are emitted from the same attribution decisions that build kill
targets: `equal_start_edge`, `unparseable_start_edge`, `inverted_start_edge`,
`foreign_root_branch`, `same_root_other_agent_branch`, `shell_boundary`, `unknown_root_cli`,
`pid_reuse_suppressed`, `legacy_unverifiable_dropped`, `prior_ttl_expired`,
`prior_field_missing`, `prior_request_mismatch`, `snapshot_unavailable_no_descendants`,
`torn_provenance_read`, `foreign_launcher_suppressed`, `launcher_nonce_missing_state`,
`launcher_nonce_unsupported_argv`, `launcher_nonce_cmdline_unreadable`, `launcher_nonce_absent`,
`launcher_nonce_mismatch`, `launcher_nonce_malformed`, `launcher_nonce_duplicate`,
`launcher_nonce_after_subcommand_or_tail`, and `launcher_wrap_parse_failed`. Every kill target
carries a `reason`/`source` so reviews can trace why it was included.

`managed_pids` migration is intentionally fail-closed. New entries are versioned as
`process_ownership_v1` and include exact `root_key`, `agent`, explicit `request_id` (including
JSON null for ordinary supervised agents), `pid`, `start`, `source`, capture/fresh epochs, and
`seed_descendants`. Snapshot-unavailable cleanup uses only TTL-valid exact priors and never starts
new descendant traversal. Legacy unversioned rows are re-derived only if independently attributable
on the current tick; otherwise they are dropped from next state and counted. The residual one-tick
self-healing gap is accepted: unverifiable legacy rows are intentionally lost, while healthy
verifiable rows are re-tagged.

Accepted limitations: the strict-edge proof assumes wall-clock start ordering is monotonic across
PID reuse; a backward NTP/VM clock step that straddles PID reuse is outside this accidental
cross-project-kill fix. Deliberate parent PID spoofing with
`PROC_THREAD_ATTRIBUTE_PARENT_PROCESS` is also out of scope. This is a correctness boundary against
accidental cross-project kills and stale PID/PPID reuse, not a security boundary against a malicious
local process. The launcher nonce is likewise an accidental-collision marker visible in
`Win32_Process.CommandLine`, not a secret; a same-user malicious process that can read or spoof
command lines remains out of scope.

## P2 · PLANNED — supervisor: don't give up on a rate-limited agent (2026-07-04)

Observed live: dev-2 (wrapped Claude, mid v0.59.0 build) hit a Claude rate-limit and STOPPED
COMPLETELY (Claude agents halt on a rate-limit and do not auto-resume when it clears). Its
wrapper stayed alive but never completed a turn → never heartbeated → the supervisor relaunched
3× without reaching readiness → `READINESS_GAVE_UP` (sticky; manual intervention). The operator
normally has to hand-wake such agents. GAP: the supervisor treats a rate-limited/outage agent
(`health=rate_limited_or_outage`) like a dead one and eventually gives up, instead of HOLDING
while health says rate-limited (infra, not poison) and/or auto-relaunching once it clears. FIX:
the readiness-give-up path (supervisor.py ~1116-1136) should NOT fire while
`health=rate_limited_or_outage` (extend the existing health-working delay at ~1074 to cover the
rate-limited state); optionally an auto-wake that re-arms a recovered agent. RECOVERY used now:
`agenttalk request-restart --for <agent>` clears the give-up + relaunches (the programmatic
"wake up"). RELATED: relaunch the running team on **v0.58.4** so work-heartbeat stops the
long-silent-turn false-STUCK churn that compounds this (a relaunch also refreshes the wrappers to
current code).

## P3 · IN PROGRESS (v0.59.3 dashboard increment) — capacity display, density, and write-spine P3 closeouts (2026-07-04)

Dashboard increment 1 closes the four open v0.59.0 write-spine P3s, adds
per-provider capacity display, and makes compact density materially smaller.
None of the P3s is an auth bypass; the write boundary is sound. v0.60.0
operator inbox work remains out of this increment.

STATUS: in progress for `v0.59.3`.

- **P3 · non-ASCII CSRF header → TypeError** (sharpest). `web.py _handle_intent_post` runs
  `hmac.compare_digest(supplied, csrf_token)` on STRINGS; a non-ASCII header (latin-1 decoded,
  0x80-0xFF attacker-reachable) raises `TypeError` → unhandled `http.server` traceback + aborted
  connection. FAIL-CLOSED (no mutation, before any write) but an unhandled exception on attacker
  input on the security path. FIX: compare `.encode()` bytes, or `except TypeError` → 403 bad_csrf.
- **P3 · rate-bucket unsynchronized RMW.** `web.py _rate_allowed` mutates the shared token-bucket
  dict with no lock (ThreadingHTTPServer); two concurrent CSRF-valid requests can both admit past
  the burst ceiling. NOT an auth bypass (valid CSRF required; hard `INTENT_MAX_ACTIVE` caps hold
  under `_config_lock`). Flagged by BOTH the adversarial pass and reviewer-3. FIX: `threading.Lock`.
- **P3 · audit-eviction filename order.** `store.rotate_intents` byte-cap eviction iterates
  `sorted(iterdir())` = filename (`wi-<hex>`) order, not mtime → can drop NEWER audit records first,
  contradicting the oldest-first docstring. Audit-retention only. FIX: sort by mtime.
- **P3 · executor drain kill-switch (defense-in-depth).** the pure `intents.drain_intents` relies on
  the CLI gate + PS `Assert-ActionsEnabled` for the kill-switch; a direct programmatic caller would
  send despite an active `supervisor.kill`. Not web-reachable. FIX: short-circuit `drain_intents`
  when `supervisor_kill_switch()` is active.
- **Capacity display + density polish.** Add isolated, advisory provider-budget refresh for wrapped
  agents, richer `/api/state` capacity fields, a visible detail-card budget view, and compact-mode
  sizing that shows materially more content at 1280px without desktop/mobile overflow.
  Known limitation: manual Codex capacity reads against a shared operator `~/.codex/sessions`
  use bounded rollout discovery (`CODEX_ROLLOUT_SCAN_LIMIT=256`, `CODEX_ROLLOUT_MAX_FILES=8`).
  If a requested thread is outside the proven candidate set, capacity degrades to unknown rather
  than publishing another thread's observed budget. Supervised wrapped Codex remains isolated to
  that agent's `CODEX_HOME/sessions`.

## P2 · IN PROGRESS (v0.60.0 operator inbox) — answer escalation intent (2026-07-04)

Add the first operator-inbox write action: an `answer_escalation` intent that queues an
operator answer for a pending `needs_operator` escalation, then the executor re-derives
the recipient at drain time and emits a normal non-control message back to the requester.
This increment does **not** add attention dispositions; defer/dismiss/resolve remain a
fast-follow for v0.60.1.

Kill-switch invariant for this increment: the web tier rejects new queued writes while
`supervisor.kill` is active, the executor pauses without mutating queued intents, and any
queued `answer_escalation` is fully revalidated when the switch clears. If the escalation
was answered/closed/rescinded or the operator-facing actor drifted during the pause, drain
fails closed with zero sends; the kill-switch never purges queued intents by itself.

### Historical review record — shipped in v0.59.1 / `9d88dc1`

Second, independent fresh review of the v0.59.0 write spine (`5b8203a..72660b3`): 2 codex +
2 claude, distinct adversarial lenses, every Claude P0/P1 adversarially refuted. **Core is
VERIFIED sound in code** (OFF byte-identical, POST gate order with no pre-write side effect,
Host/DNS-rebind, constant-time CSRF, server-side actor derivation with no browser identity
trusted, no-double-send crash recovery, confirmed-dead-only reclaim, kill-switch 423, preflight
read-only). One real code gap + a test-honesty cluster. Shipped in `v0.59.1` /
`9d88dc1`. None is browser-reachable; v0.59.0 stayed live during the fast-follow.

- **P1 · drain-time plan-trust bypass (defense-in-depth / invariant-integrity)** — codex-reviewer-1,
  REPRODUCED at 72660b3. `intents._drain_one` validates only `rec.kind`/`rec.payload` (intents.py:388),
  then if a frozen `plan` already exists and `plan.actor == resolved actor` (intents.py:400/414) it
  feeds the plan's `bus_kind` + `stable_meta` straight into `store.send` (intents.py:451-462) with NO
  re-check against the closed bus-kind map / reserved-meta rules. A co-resident who writes a crafted
  plan into `state/intents/active/<id>.json` (`bus_kind=release` + control meta) gets an
  operator-authored `release` on the bus. NOT browser-reachable (`store.write_intent` never accepts a
  browser plan) → not a pull; but it violates the "executor re-validation is the SOLE gate vs a
  store-writing co-resident" invariant (design critique-must-fix #4). FIX: re-validate a trusted frozen
  plan at drain time (bus_kind ∈ allowed map, stable_meta passes reserved-meta), fail-closed to
  INTENT_DENIED with a structured code, WITHOUT discarding the freeze (resumable fan-out). The claude
  test-coverage lens independently predicted this exact risk class.
- **P1 · no negative CSRF test** — claude-reviewer-B, verify-confirmed P1. The `hmac.compare_digest`
  gate is correct in code, but no test sends a WRONG-but-present token or asserts `bad_csrf`; a
  regression to prefix/`startswith` matching would pass CI. FIX: wrong-token POST → 403 `bad_csrf` +
  tree unchanged; pin `problem.error` on the existing missing-token test.
- **P2 · executor defense-in-depth test** — a raw file dropped directly into `intents_active_dir` with
  a control bus_kind / reserved meta / unknown kind must drain to INTENT_DENIED, zero sends. This test
  ALSO pins the plan-trust P1 fix (drop a crafted PLAN → denied). (claude-reviewer-B, downgraded from P1.)
- **P2 · no-forgery e2e test** — POST carrying a top-level browser `from=<x>`, drain, assert
  `message.sender == resolve_web_actor` (derived), never the browser value. (claude-reviewer-B, downgraded.)
- **P2 · pid_start-ignored-on-reclaim** — codex-reviewer-2, REPRODUCED. `claim_intent` (store.py:3702)
  and `claim_supervisor_instance` (3831) reclaim on `_process_liveness(pid)` alone, ignoring the recorded
  `pid_start`; PID reuse before the stale threshold strands the intent/singleton behind an unrelated live
  process. FIX: pid_start-aware reclaim (same pid + different start == confirmed gone) + deterministic
  PID-reuse regression for both the intent claim and the instance lock.
- **P2 · torn-active-intent skipped forever** — codex-reviewer-2, REPRODUCED. On the Windows sandbox
  direct-write fallback a partial `active/<id>.json` reads None, is skipped by `list_intents`/drain
  forever, never terminalized, still counts against the active cap. FIX: bounded recovery/quarantine
  (mirror `prune --invalid`) or at minimum doctor surfacing + a rotate path. Treat as ONE quarantine
  story with the unparseable-timestamp P3 below.
- **P2 · origin-mismatch / rate / body / intent-cap / no-double-drain / drain-denial-code tests** —
  claude-reviewer-B. The gates are code-correct but have no negative web-layer tests: foreign Origin →
  403 `bad_origin`; burst>limit → 429 `rate_limited`; body>64KiB → 413 `body_too_large`; active-cap →
  429 `intent_cap`; a second live singleton claim refused + two overlapping drains → exactly one message;
  drain-time codes (`target_not_in_roster`, `reply_anchor_not_found`, `empty_audience`,
  `multiple_leads_configured`, `actor_changed`) each pinned.
- **P3 (bank; fold only if trivially cheap):** terminal intents count against the active cap during the
  600s linger; reset-mid-drain ghost-send (`update_intent` no-ops but `store.send` still fires);
  actor-changed partial-fan-out duplicate on requeue (new intent_id defeats the crash-recovery
  fingerprint); reply-anchor onto a non-proposal message (CLI parity); `rotate_intents` skips a record
  whose `terminal_at`/`created_at` is unparseable forever (age None → never rotates — sibling of the
  torn-file P2); OFF-mode POST not full-tree-hash-verified.

## P2 · PLANNED (fast-follow) — wrapper stream teardown + work-heartbeat P3s

Small, low-risk, bundle into one dev-2 task once capacity returns (normal cadence).

- **P2 · Windows Errno-22 teardown spam.** A wrapped Claude window spammed `OSError: [Errno 22]
  Invalid argument` / `Exception ignored while finalizing file <_io.TextIOWrapper name=3>`
  pointing at `run.py:1359` (`for line in stream`). DIAGNOSIS: the loop's `try/except OSError`
  (`run.py:1357`/`:1394`) already catches ITERATION-time OSErrors; this spam is FINALIZER-time —
  `_ProcStream.__iter__`'s `finally` closes the child stdout pipe (`run.py:1211-1213`,
  `self._proc.stdout.close()`), and on Windows closing an already-torn-down overlapped pipe
  raises EINVAL (Errno 22) during generator GC, so it cannot propagate and prints as "Exception
  ignored." FIX: wrap the pipe teardown in that `finally` (the `stdout.close()` at `:1212`, and
  best-effort the `yield from` at `:1199`) in `try/except OSError` / `contextlib.suppress(OSError)`.
  Benign (nothing at risk; recycling the window clears it) but noisy. Credit: the operator's lead
  localized it to `:1359` / the stream teardown. WHERE: `src/agenttalk/wrapper/run.py:1199,1211-1213`.
- **P3 · non-bool `enabled` silently ignored** (banked from the v0.58.4 work-heartbeat review):
  `resolve_work_heartbeat._flag` requires an actual bool, so a JSON string `"false"` neither
  disables nor errors — it falls through to the default (ticker stays ON for wrapped Claude),
  inconsistent with the never-coerce posture the numeric keys get. FIX: record a `config_error`
  for a non-bool `enabled`. WHERE: `src/agenttalk/wrapper/work_heartbeat.py` (`_flag`).
- **P3 · generator-finalization dependency doc** (banked): the abnormal-exit stop-before-cleanup
  ordering relies on CPython synchronous generator finalization — SUPERSEDED by the Errno-22 fix
  above (same teardown path); fold together, add the documenting comment there.

## P3 · PLANNED (post-v0.59.0) — dashboard UI polish

Small console.css/js polish; run as a focused pass AFTER v0.59.0 ships — keeps the write-spine
security review clean and avoids editing console.css/js while dev-2 is mid-build in those files.

- **Compact density is a near-no-op.** The comfy/compact toggle IS wired (console.js:517-522 →
  `applyPrefs` sets `#app[data-density]`, same path as the working theme toggle), but
  `[data-density="compact"]` changes only ONE variable — `--card-pad` `15px 16px` → `13px 14px`
  (console.css:141-142), a ~2px delta — so it reads as a dead button. FIX: have `compact` drive
  MORE dimensions (row/list padding, inter-card gaps, header/nav height, maybe font-size) via
  additional density-scoped custom properties so it visibly tightens. WHERE:
  `src/agenttalk/web_static/console.css` (density vars) + `console.js` if new vars need wiring.
- (Running bucket for further console UX nits the operator surfaces while using the dashboard.)

## PLANNED (feature) — per-provider usage/limits in the dashboard (operator ask 2026-07-03)

GOOD NEWS: mostly already built. `capacity.py` extracts 5-hour + weekly rate-limit windows +
context for BOTH providers — Claude from `~/.claude/statusline-last-input.json`
(`rate_limits.{five_hour,seven_day}`), Codex from the newest `~/.codex/sessions/**/rollout-*.jsonl`
`payload.rate_limits.{primary,secondary}` (capacity.py:18-22,113-249) — i.e. agenttalk already
reads the Codex rate limits the operator's own statusline tool could not. `/api/state` carries
per-agent `capacity`; console.js already renders a 5-hour rate meter + context meter
(console.js:402-404,1321-1324).

GAP (why Codex looks empty): capacity is SELF-published by each agent's skill
(`agenttalk capacity refresh`), NOT auto-refreshed by the wrapper — `wrapper/loop.py` has NO
capacity wiring — so snapshots go stale/absent, especially for codex. OPEN QUESTION to verify
first: does a WRAPPED HEADLESS agent produce its provider source? codex writes rollouts (yes);
a wrapped `claude -p` may NOT run the statusline that writes `statusline-last-input.json` (verify).

FIX: (1) auto-refresh capacity in `wrap --loop` each turn (loop.py, both providers, fail-safe) so
every supervised agent always has fresh usage without depending on its skill; (2) enrich the
console to show BOTH 5h + weekly windows + `resets_at` + `plan_type`, per agent + a per-provider
summary (console.js/css — FOLD with the dashboard work to avoid conflict; the loop.py auto-refresh
is independent and can ship on its own). Design-first dispatched to dev-4 (assignment=dashboard-capacity).

## SHIPPED v0.57.0 — supervisor hardening · v0.57.1 — attention fast-follow (2026-07-02)

- **v0.57.0** — kill-switch (`.agenttalk/supervisor.kill`), an independent fail-closed
  `agenttalk deadman` mail-age alarm, a durable Scheduled-Task host (`supervisor-task.ps1`,
  `docs/supervisor-hosting.md`), start-time-guarded kills + heartbeat-based restart
  reconciliation. 3/3 reviewed, lead-gated, CI-green.
- **v0.57.1** — attention-queue fast-follow: `attention --stats` (+ `--json`) north-star
  counts, `show --include-*`, wrapped-form typed validation, naive-as-UTC, requeue/resolve
  guidance. Review fold: **F8** (reviewer-1) a shared `_attention_input_warnings` so `--stats`
  carries the same degraded-input warnings as the queue view; **F9** (codex) honest
  no-body-reads docs.

## Findings from the first real supervised run (2026-07-02)

Origin: dogfooding the hardened supervisor on the live team (claude lead + 5 codex + 3 claude
workers, all `wrap --loop`, real auto-recovery). The supervisor revived a dead Claude dev
(dev-2) by itself — the revival premise works — but the first real dev turn surfaced these.

- **P1 · PLANNED · wrapped-Claude has no write grants.** A supervised `wrap --loop --cli
  claude` child ran in DEFAULT permission mode, so every Edit/Write/git write was auto-denied
  headless (main repo AND worktree); the agent could read and use the bus but could not
  edit/commit. Root cause: the wrapper never applies `claude_permission_mode` to the child
  argv — it relied on a seeded `.claude/settings.json defaultMode`, which does not grant
  writes for a `-p` headless child. WHERE: `src/agenttalk/wrapper/run.py` (no
  `--permission-mode` in the claude argv); `supervise --seed-claude-settings`. INTERIM FIX
  (config, applied): add `--permission-mode bypassPermissions` to each Claude worker's `wrap`
  tail (+ `--add-dir <worktree>` when the work is outside cwd). PROPER FIX (planned): the
  wrapper applies the resolved `claude_permission_mode` to the child argv so the seed and the
  flag cannot diverge. (Codex agents were unaffected — they already run `-a never -s
  workspace-write`.)
- **P2 · KNOWN LIMITATION · isolated worktree vs sandboxed agents.** A separate-worktree task
  (worktree files, plus git metadata under the main repo's `.git/worktrees/…`) falls outside
  a sandboxed agent's writable root — Codex `workspace-write` + `writable_roots` (the Orbit
  team hit this on `git worktree`) and Claude cwd/permission-mode are two faces of one thing.
  Options, safest last: drop the sandbox (Codex `danger-full-access` / Claude
  `bypassPermissions`); widen the writable root (`writable_roots += worktree` / Claude
  `--add-dir`); or keep worktrees INSIDE the repo (or work on branches in-cwd) so nothing
  leaves the writable root. RECOMMENDATION: prefer in-cwd branches / in-repo worktrees for
  supervised agents; consider having the supervisor auto-add the active worktree to
  `writable_roots` / `--add-dir` when it seeds.
- **P3 · BACKLOG · supervisor `.ps1` hot-config-add crash.** The generated `supervisor.ps1`
  caches `$cfg` at startup but `supervise --plan` reads the live config each poll, so ADDING
  an agent to a running supervisor's config makes the state write-back
  (`$state.agents.$name = …`) throw on a `PSCustomObject` that lacks the new property (seen
  live when the alert-only loop met a 10-agent config). WORKAROUND: restart the supervisor
  after any config change (standard). FIX: seed `$state.agents` from the plan's agents via
  `Add-Member`, or reload `$cfg` each poll.

## SHIPPED v0.42.0 — split-identity lead-loop enforcement (Slice 1 + WP1–4)

Origin: the **operator-raised "the lead stops leading"** failure (every lead, every
project): a chat-agent lead silently UN-ARMS its control loop — the harness default
("answer the human, then yield") drops the re-arm even though the discipline is
documented and in memory. A documented-but-reliably-dropped rule means willpower is
NOT the fix; we need MECHANICAL enforcement. The operator chose **split-identity
enforcement** (cli-AGNOSTIC — keyed on the agent NAME + its `managed_lead_loop`
config, never a cli, so a codex identity is managed exactly as a claude one): keep the
free-form `<name>` as the operator-facing liaison (manual, never auto-killed), and add
a separately-supervised managed `<name>-lead-loop` identity that OWNS the team mailbox
via a renewable lease and cannot silently un-arm. codex designed it as Slice 1 + four
WPs; dev-2 built each in an isolated worktree; codex + reviewer-1 cross-reviewed; lead
gated. Rationale + decision log: `docs/DESIGN.md` D-12..D-15.

- **Slice 1 (61e2ce0) — store-level lease + single-consumer guard + visibility.**
  `state/<agent>.lead-loop-lease.json` is the ownership truth (acquire/renew/release/
  read; `.waiting` only mirrors it for UX); a verb-guard rejects a second mailbox
  consumer (`wait`/`recv`/`drain`/`ack`, exit 7) while read-only verbs stay allowed,
  the owner proving itself via the live `lease_id` (`AGENTTALK_LEAD_LOOP_LEASE`);
  `lead_unarmed` is an ERROR in `status`/`doctor`/supervisor for a managed identity
  that is down. Steal is gated on the configured `managed_lead_loop` flag + a
  CONFIRMED-dead tri-state liveness probe (D-12): a confirmed-dead owner is stolen
  immediately (a crashed controller recovers at once), a live/uncertain one only when
  EXPIRED *and* heartbeat-stale (an uncertain probe never displaces a live owner; a
  manual chat identity is never auto-stolen).
- **WP1 (24c39f7) — single authority + timing resolver + corrupt-config class.**
  One `_lead_loop_authority` computes {managed, present, liveness, expired,
  heartbeat-stale, stealable, armed, guarded, reason}; `_lease_stealable`,
  `lead_loop_state`, and `lead_loop_active_owner` ALL derive from it, so the three
  views can never drift (for a present managed lease `armed == not stealable` in
  every case). `read_lead_loop_lease` normalizes `expires_at` to a finite float or
  None (fail-safe NOT-expired). A timing resolver (`lead_loop_runtime.resolve_timing`)
  gives the steal path and the visibility paths one shared `heartbeat_stale_after`
  (a duplicate never steals earlier than the supervisor would call the owner stuck),
  keeping `store.py` free of supervisor imports. A truthy non-dict supervisor config
  entry (operator typo) coerces to default via `isinstance` instead of crashing
  status/doctor/supervise/wrap — the corrupt-config coercion class swept across every
  per-agent reader (D-13).
- **WP2 (152636e) — the lead-loop CONTROLLER (`wrap --loop --lead-loop`).** A
  long-running supervised process that owns the mailbox for its whole lifetime:
  acquire-before-loop, a combined renew+heartbeat on every idle stamp and streaming
  event, an ownership gate at every cursor-advance boundary (a lost lease stops
  consumption at once), and three exit states the supervisor reads from an exit
  marker — blocked-acquire (HOLD, no relaunch), valid human release/end (deliberate
  stand-down, no relaunch), crash/lost-lease (relaunch + re-acquire). The lease token
  is never leaked to the model child.
- **WP3 (fe6ed6b) — the proactive CADENCE TICK.** The idle/timeout branch of the
  SAME loop (no second consumer/thread) drives a SYNTHETIC sweep over a bounded
  read-only snapshot (ids + summaries, never transcripts, never the lease token) when
  the bus is quiet and the cadence interval elapses — nudging stalled outbound threads
  (once per `(request_id, last_msg_id)`, past `reminder_after_seconds`, no fresh-peer
  composing marker) and surfacing dead-letter / unrouted escalations (deduped),
  spending a model turn only when something is actionable. It NEVER records an attempt,
  advances the cursor, or enters the dead-letter path; a failed sweep is
  controller-HEALTH (backoff + heartbeat withheld + a single deduped escalation
  retried until it routes), never message poison (D-14).
- **WP4 (de2873a) — the mechanical liaison RELAY (`agenttalk relay`).** Carries the
  operator's words across the human<->bus boundary with an audit stamp and NO new
  message kind: `relay operator-answer --to-request <rid>` validates a *pending*
  `needs_operator` escalation addressed to the liaison and routes the answer back to
  the asking lead-loop (flipping the thread to `operator_state=answered`); `relay
  operator-command` relays a spontaneous operator instruction to a managed lead-loop,
  inferring `--to` only when exactly one exists and FAILING CLOSED unless the sender is
  the operator-facing liaison (an audited `--override --reason` is the only exception).
  Both handlers are authoritative for the reserved audit meta — a caller `--meta` can
  never forge an audit marker or graft routing onto a relayed message (D-15). The
  lead-loop → operator direction stays the existing `escalate`; no new kinds.
- **Cadence-snapshot threshold-skew fix (8b8e79e) — the v0.42.0 release blocker.**
  reviewer-1's consolidated strict-2/2 review of the merged arc caught that
  `build_cadence_snapshot` resolved the supervisor heartbeat window into
  `snap["timing"]` but built `snap["lead_loop_health"]` via `lead_loop_state(agent)`
  WITHOUT it, so the cadence health view fell back to the 120s default while
  steal/guard/supervisor used the resolved window (e.g. 900s) — the *same*
  threshold-skew class WP1 set out to kill, resurfacing in the visibility snapshot,
  where it could hand the model a FALSE controller-down state while the controller
  still owned the lease. *Fix:* thread `now=now_epoch` + the resolved
  `heartbeat_stale_after` into the health call (None → safe default), inside the
  existing degrade-guard; pinned by a WP3 regression
  (`test_snapshot_health_uses_resolved_window_not_default`) that fails unfixed /
  passes fixed.

*Where:* `store.py` (lease API + `_lead_loop_authority` + `_process_liveness`
tri-state + read normalization + confirmed-dead-only lock-break + cadence-state),
`lead_loop_runtime.py` (NEW, timing resolver), `lead_loop_cadence.py` (NEW, snapshot +
actionability + cadence state + health), `wrapper/loop.py` + `wrapper/run.py` +
`wrapper/prompt.py` (controller + per-cursor ownership gate + token-strip + combined
heartbeat + the idle-branch cadence hook), `cli.py` (`wrap --lead-loop`,
`managed-lead-loop` set/clear/list, the verb-guard, `cmd_relay` +
`_RELAY_RESERVED_META`), `supervisor.py` (`_plan_one` no-relaunch rules + report
field), `doctor.py` (`_check_lead_unarmed`), `docs/DESIGN.md` (D-12..D-15),
`tests/test_lead_loop*.py` + `tests/test_relay_wp4.py` + `tests/test_supervisor.py`.

Process: each WP merged slice-internal on codex review + lead adversarial-verify +
lead full-suite gate (the independent-verify-different-focus pattern caught a real bug
at nearly every WP — config-brick, the stale-blocked-marker recovery defeat, audit-meta
forgeability — complementary to codex's resolver-skew / lost-lease / escalation-latch /
correlation-id catches). The v0.42.0 release added reviewer-1's consolidated strict-2/2
on the exact head, which caught the cadence-snapshot threshold-skew blocker; dev-2
folded it (8b8e79e); BOTH reviewers re-approved the fix on the final SHA.

Status: **SHIPPED as v0.42.0** (merge SHA `8b8e79e`). Strict 2/2 on the final SHA
(codex + reviewer-1, no findings) + lead-gated (ruff/bandit/diff-check clean, **1395
passed on Python 3.10 AND 3.14**).

**Accepted limitation (WP1): the triple-fault edge.** An owner whose liveness probe
is UNKNOWN *and* whose lease has a corrupt/None expiry *and* whose heartbeat is stale
is NOT stealable (normalized None = not-expired), so a dead-but-unprobeable owner
with a corrupt lease waits for a valid renewal it cannot make. This is the deliberate
fail-safe direction: NEVER a false steal of a maybe-live owner; delayed recovery only
under a triple fault. Pinned by a regression test.

**Accepted limitations (Slice 1).** *PID-reuse in the liveness heuristic (P2):*
steal / guard / armed use the tri-state `_process_liveness(owner_pid)` (D-12); a
recycled pid can make a dead owner look ALIVE, but the `lease_id` (not the pid) is the
real ownership token and a recycled pid won't heartbeat as this agent, so the worst
case is delayed (expiry+heartbeat) recovery, NEVER a false steal of a live owner.
*Lease + `.waiting` mirror are not atomically coupled (P3, by design):* the lease
write is atomic, the observational mirror is best-effort; a crash between them leaves a
valid lease without a mirror and readers degrade.

**Slice 1b (post-turn turn-end audit) — DEFERRED.** A post-final-answer harness hook
to verify the lead re-armed a background wait is not buildable on the current host
(only soft PostToolUse hooks exist, e.g. `agenttalk heartbeat --hook`); the managed
`lead_unarmed` detector is the substitute (a wrapped lead-loop is mechanically armed
every cycle, and the liaison no longer owns the team loop). Revisit only if the host
harness adds a reliable post-turn hook.

## SHIPPED v0.40.0 — hardening batch (`hardening-batch-040`)

Origin: the **2026-06-28 fresh-agent audit** (6 independent reviewers; the
operator asked every agent to spawn its own fresh reviewer). The audit found a
cluster of real, *shipped* false-GO / authority defects the normal cadence had
missed — validating the assurance posture. Branch `hardening-040`; designed by
codex, gated by lead, built by dev-2, cross-reviewed by codex + reviewer-1.

- **C1 · gates.py fail-closed cluster (P1).** Four distinct fail-opens, all
  letting a release report GO when it should HOLD:
  (1) `set/waive` silently overwrite a *corrupt* `gates.json` (dropped the
  corruption HOLD); (2) unlocked read-modify-write lost-update; (3) a required
  gate under a mismatched scope was dropped (absence=pass); (4) a `severity=blocker`
  gate set `status=skipped` returned GO (reproduced). *Fix:* refuse mutation on
  `load_error`; lock the full RMW; present-but-wrong-scope blocks; `skipped`≠green
  for blockers; **+ new `tests/test_gates.py`** (the module had zero dedicated
  tests). *Why it mattered:* the subsystem meant to prevent false-GO was the
  weakest link.
- **C2 · lane shared-approval authority (P1).** `_shared_approved` accepted a
  path *prefix* match, so a broad `schema/**` approval cleared a nested
  `schema/secret.sql` whose distinct approvers were never consulted; and the
  verdict never revalidated persisted approvals. *Fix:* drop the prefix arm;
  persist the matched entry glob (not raw path); revalidate at verdict time →
  emit the previously-dead `HOLD_SHARED_WRONG_APPROVAL`; and require approval from
  **every** matching shared entry — a touched path is cleared only when each
  matching entry has a fresh approval by an authorized approver (no winner-picking
  between overlapping globs; that ordering was twice unsound). Validation rejects
  duplicate normalized shared globs. 3 reviewers converged; the fix iterated
  prefix → most-specific → all-matching as review reproduced deeper bypasses (D-11).
- **C3 · wrapper one-shot + release authority (P2/P3).** One-shot reviewer could
  starve/hang behind an unrelated unread message; the loop left a stale `.waiting`
  marker on exit; `is_release_authorized` still carried a zero-lead fallback that
  diverged from the v0.39 fail-closed envelope. *Fix:* scoped receive + bounded
  timeout; `try/finally clear_waiting`; `is_release_authorized` delegates to the
  single `loop_exit_relay_authorized` resolver.
- **C6 · end-to-end regression test (operator request).** New
  `tests/test_e2e_lifecycle.py`: in-process `cli.main` over a real throwaway git
  repo, asserting exit codes + JSON verdicts + on-disk state across the full
  lifecycle, **including negative assertions that the C1/C2/C3 bugs stay fixed**
  and that `reset` preserves the durable set. Covers the previously-untested
  CLI↔core wiring, git adapter, and reset durability boundary.

Status: **SHIPPED as v0.40.0** (merge SHA `e0e8f7b`). All four clusters approved by
both reviewers and lead-gated (ruff/bandit/diff-check clean, 1213 passed on 3.10
AND 3.14). The C2 authority fix iterated through three reproduced bypasses before
review settled on all-matching (D-11).

## SHIPPED v0.40.1 — fast-follow (merge SHA `1962b92`)

Both approved + lead-gated (1227 passed on 3.10 AND 3.14). Review folded one real
C5a P1 (structural → semantic artifact readback). WP resolver deferred (banked).

- **C4 · knowledge gaps (0.38.0).** `roster --expertise` now uses the curated view;
  anchor staleness fails closed (`missing_verified_baseline` for a null path/symbol
  baseline, `unsupported_wp_anchor` for a pathless `wp`, exact `msg_id`, scan
  failure → unresolvable); `publish`+`curate` share one durable append helper
  (lock + flush + fsync, Windows-guarded); `knowledge onboard` is bounded
  (`--limit`, default 20, grouped domain→type).
- **C5 · TOCTOUs.** `lane deliver` reads back + shape/verdict-validates the delivery
  artifact before clearing the lane (a HOLD/wrong-schema artifact can no longer
  clear it); `write_restart_request` + `clear_restart_request` share the config
  lock so a stale clear cannot remove a newer marker.

## SHIPPED v0.41.0 — dead-letter / poison-message handling (`dead-letter-build`)

Origin: the 0.30.0 **poison-message head-of-line** known limitation + the
2026-06-28 backlog ("do all of them"). A message failing *deterministically* at the
head of a mailbox drove an unbounded backoff-restart loop. Designed by codex
(ultracode rubric gate), built by dev-2, cross-reviewed by codex + reviewer-1, and
lead-gated through **six** adversarial-verify passes — the 6th caught a real
classification bug that 2/2 reviewers + five passes had missed (folded before ship).

- **Durable attempt ledger + recoverable sink.** Per-agent ledger
  (`state/dead-letter-attempts/<agent>.json`), write-ahead before each drive so a
  crash mid-turn counts; on exhaustion the head message moves to a scan-invisible,
  recoverable sink (`dead-letter/<agent>/`) and the cursor advances **last** — never
  unless the bytes are recoverable; collision-safe; idempotent replay.
- **Three-way failure taxonomy.** poison-eligible (terminal turn-failed +
  crash-mid-turn) → low *consecutive* cap K=3; known-global-infra
  (spawn/auth/rate-limit/network/5xx + recognized retryable transport drop) →
  **never** auto-DL, escalate at K=20; ambiguous/unknown → escalate + auto-DL only
  at K=20. No class loops forever; a sustained outage never false-DLs at the low cap.
- **6th-verify P2 (folded, `cd39e12` → `b91e400`).** `_classify` checked the
  started/partial branch *before* the retryable→infra branch, so a retryable
  transport drop *after* the handshake (codex "Reconnecting…", claude rate-limit
  mid-stream) was misclassed ambiguous and could false-DL a healthy message at the
  ceiling during an outage. *Fix:* a recognized retryable signal classifies **infra**
  before the started branch (terminal-text precedence preserved) + regression tests.
- **Restore = requeue** (fresh-id message; no cursor rewind). **CLI:**
  `dead-letter list/show/requeue`; **doctor** loud on no/solo escalation target or
  unreadable sink; **scope:** supervised continuous loop only (manual `listen` +
  one-shot untouched — documented v1 boundary).

Status: **SHIPPED as v0.41.0**. 2/2 reviewer-approved on the frozen SHA + lead-gated
(ruff/bandit/diff-check clean, 1280 passed on 3.10 AND 3.14).

## BACKLOG

- **`send_operator_answer_atomic` extra_meta defense-in-depth (P3).** The shared
  operator-answer helper merges caller `extra_meta` then overwrites only the three
  canonical keys. Both current callers are safe (CLI scrubs control meta via
  `_RELAY_RESERVED_META`; the drain passes only shape-validated stable/executor
  meta), so a smuggled `needs_operator` cannot reach it today — and even if it did,
  terminal classification ignores the reply's meta. Banked from the v0.60.1
  adversarial/reviewer-2 notes: if this helper ever becomes a broader public API,
  scrub control/reserved keys inside `Store` as defense-in-depth.
- **`relay operator-answer` `not_found` message nuance (P3, trivial).** The CLI maps
  a resolver `not_found` to a "no thread / not your thread" message, but `not_found`
  also covers "thread has no validated opener" — a rare-case wording nuance, no
  correctness/authority impact (v0.60.1 adversarial note).
- **Dead-letter defense-in-depth (P3, fast-follow).** Banked from the dead-letter
  review/verify: (1) `ack` / `advance_cursor` accept an arbitrary id on write (no
  `_ID_RE` guard) — an operator cursor-skip vector; (2) the disposal path is not
  wrapped against a transient IO error mid-move (fail-closed today, but a retry +
  backoff would be more robust); (3) no idle/startup reconcile of a stuck
  `in_progress` / orphan-sidecar, and `doctor` does not warn on it.
- **Model-tiering / routing.** Route work to model tiers by task class.
- **Restart resilience.** Restart-notice, checkpoint-before-compact skill,
  richer `request-launch`.
- **Auto-provision per-agent git worktree** for supervised/parallel dev (the
  harness already has a worktree-isolation concept) — removes the manual
  isolated-worktree step from the cadence.

## KNOWN LIMITATIONS (shipped & accepted)

- **Poison-message head-of-line blocking** — fixed for the supervised wrapper loop
  in v0.41.0 (dead-letter). Manual `listen` and one-shot turns remain out of v1
  scope (documented boundary).
- **Wrapped-Codex conservative 900s stale threshold** — chosen for safety; may
  delay degraded detection for wrapped Codex.
- **No explicit visible idle signal** (minor UX).
- **`degraded.py` `window_seconds >= stuck_after` invariant** is false for
  wrapped Codex (900s) — latent, telemetry-only, no live mis-fire (P2, banked).
- **Defense-in-depth nits (P3, banked):** state helpers don't re-`validate_agent_name`
  (CLI validates upstream); `_process_alive` treats exit 259 (STILL_ACTIVE) as
  alive; `domains` normalize allows trailing dots / reserved device names (not a
  path escape); ephemeral `skill`/`profile` weak validation (not shell-exploitable;
  layered behind disabled-by-default + authorized-lead).

## Audit findings → disposition (2026-06-28)

Full point-in-time report (methodology, per-reviewer detail, what-held): `docs/audit-2026-06-28.md`.

| # | Finding | Sev | Reviewers | Disposition |
|---|---|---|---|---|
| 1 | gates corrupt-overwrite false-GO | P1 | security | C1 / 0.40.0 |
| 2 | gates unlocked RMW | P1 | security, reviewer-1, dev-2 | C1 / 0.40.0 |
| 3 | gates scoped-drop absence=pass | P1 | dev-2 | C1 / 0.40.0 |
| 4 | gates skipped-blocker→GO (reproduced) | P1 | reviewer-1 | C1 / 0.40.0 |
| 5 | gates: no `tests/test_gates.py` | P0(cov) | test-coverage | C1 / 0.40.0 |
| 6 | lane shared-approval over-grant + no revalidation | P1 | dev-2, test-coverage, reviewer-1 | C2 / 0.40.0 |
| 7 | lane overlapping-glob authority (all-matching-must-approve) (reproduced) | P1 | reviewer-1, codex | C2 / 0.40.0 |
| 8 | `kind=end` from any sender | P1 | dev-2 | **fixed in v0.39.0** |
| 9 | wrapper release-authority drift | P1→cleanup | codex/Kepler | C3 / 0.40.0 |
| 10 | wrapper one-shot starve/hang | P2 | dev-2, reviewer-1 | C3 / 0.40.0 |
| 11 | wrapper leaves `.waiting` on exit | P3 | codex/Kepler | C3 / 0.40.0 |
| 12 | e2e regression test (operator ask) | — | test-coverage | C6 / 0.40.0 |
| 13 | knowledge `roster --expertise` wrong view | P2 | correctness | C4 / 0.40.1 |
| 14 | knowledge wp/request/null-sha anchor fail-open | P2/P3 | reviewer-1, correctness, Kepler | C4 / 0.40.1 |
| 15 | knowledge dup writer / no fsync | P2 | Kepler | C4 / 0.40.1 |
| 16 | knowledge `onboard` doc drift | P3 | correctness | C4 / 0.40.1 |
| 17 | close publish unlocked TOCTOU | P2 | security | C5 / 0.40.1 |
| 18 | lane deliver direct-write no readback | P2 | dev-2 | C5 / 0.40.1 |
| 19 | clear_restart_request TOCTOU | P2 | dev-2 | C5 / 0.40.1 |
| 20 | P3 defense-in-depth (×3) | P3 | dev-2/Kepler | banked |
| — | ephemeral skill/profile validation | P3 | security | banked |

None were remotely exploitable or recall-worthy (they require pre-existing
corruption, specific mis-use, or are conservative/advisory).

## Recently shipped (rationale in CHANGELOG.md / docs/DESIGN.md)

- **v0.42.0** — split-identity lead-loop enforcement: managed lease + single-consumer
  guard, single authority + timing resolver, the supervised controller, the proactive
  cadence tick, the mechanical liaison relay; fixes "the lead stops leading"
  mechanically. [D-12–D-15]
- **v0.40.1** — fast-follow hardening: knowledge expertise curated-view, anchor
  staleness fail-closed, one durable writer, bounded `onboard`; lane delivery
  artifact verified-before-clear; restart-marker lock.
- **v0.40.0** — post-audit hardening: gates fail-closed, lane all-matching shared
  approval [D-11], wrapper one-shot + resolver unification, first e2e regression
  test. (Origin: the 2026-06-28 fresh audit — `docs/audit-2026-06-28.md`.)
- **v0.39.0** — stand-down authority (idle = always listening; human-origin
  loop-exit envelope). [D-7]
- **v0.38.0** — knowledge layer (append-only pointer memory; capture-open +
  curate-gated; anchor-relative staleness). [D-6, D-8]
- **v0.37.0** — lane deliver-gate (middle-tier Phase 1).
- **v0.36.0** — ephemeral adversarial reviewers (evidence-only). [D-9]
- **v0.31.0–v0.35.0** — domain registry + the assurance arc (gate, close,
  sign-offs, devkit skills).
- Full history: `CHANGELOG.md`.
