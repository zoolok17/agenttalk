# Design: supervisor liveness-model redesign (0.28.1 blocker)

Status: draft (design-review round 4 — codex round-3 ruling folded in)
Date: 2026-06-18
Author: claude-agenttalk-developer-2
Reviewer: codex (authoritative on the Codex process model)
Assignment thread: `a-liveness-design`
Related: the merged launch-layer rework (`ca13a42..8b25b15`, CI hotfix `bbec73a`).

## Summary

The Phase-1 live test proved the launch layer works (shim/PATH, native-exe
dispatch, `$agenttalk-listen` auto-entry) but the **liveness/PID model is
broken**: `codex.exe` is a *forking launcher*. `Start-Process -PassThru`
records the launcher PID, which spawns the real Codex TUI (and, under it, the
command-runner and the `agenttalk wait` python), then **exits**. The current
model monitors that one PID, so every 15s poll sees "dead" and relaunches —
a new window every 15s (relaunch storm). Worse, after the operator closed the
windows, an orphaned `agenttalk wait` kept heartbeating (`last_seen=0s`), so a
naive heartbeat check is *also* fooled (zombie).

This design replaces the single-PID liveness model with a **process-tree
liveness model**: discover and monitor a long-lived *brain* PID, treat the bus
heartbeat as one input (never the sole signal), and classify each agent into
one of its liveness states. The decision core stays pure Python (`plan_actions`); the
`.ps1` stays a thin executor that supplies a process snapshot and performs
kills. The merged launch layer (`launch.windows_file`/`windows_args`) is kept
as-is; this work **adds** state fields + discovery rather than ripping it out.

## Goals / non-goals

Goals:
- Never relaunch an agent that is actually alive (kill the storm).
- Never be fooled into "healthy" by an orphaned `wait` heartbeat (kill the
  zombie).
- Preserve context across a supervisor-driven relaunch (Codex: per-agent
  isolated `CODEX_HOME` + `resume --last`; operator approved FULL scope).
- Keep the decision core deterministic and unit-testable from synthetic
  process snapshots (no real process death in CI).
- Be CLI-agnostic: the same state machine must serve Codex today and Claude
  once Phase 2 verifies Claude's fork behavior.

Non-goals (v1):
- Windows Job Objects for kill containment (could clash with Codex's own
  sandbox/job machinery — follow-up only; see §11).
- Recovering context for *manually* launched agents. Only an agent the
  supervisor launched has a tracked launcher/brain/session, so
  context-preserving relaunch is only possible for supervisor-launched
  agents. The live test must let the supervisor launch a fresh test agent.

## Authoritative process model (Codex — cited, designed around)

Per agent the supervisor tracks:
- `launcher_pid` — the short-lived bootstrap PID from `Start-Process -PassThru`.
  Used ONLY during the launch grace window; its death is expected, not failure.
- `brain_pid` — the discovered long-lived Codex TUI process. **This is THE
  managed PID; it is MANDATORY for a `healthy` classification.**
- `managed_pids` — last-seen descendant set (command-runner(s) + the
  `wait`/python). Used for the recursive kill and zombie detection.
- `resume_available` — whether this agent has reached readiness in its isolated
  `CODEX_HOME` at least once (so `resume --last` is safe). Replaces the Codex
  rollout-UUID `session_id`, which the round-3 ruling DROPPED (isolated home
  makes `--last` unambiguous).
- `last_launch_epoch`, `launch_grace_until`, `consecutive_fails`.
- **A start-time is persisted with every PID** to defeat PID reuse (a recycled
  PID with a different start-time is NOT our process).

Launch flow (Codex): start `codex.exe` -> record `launcher_pid` +
`launching=true` -> enter a 60–120s GRACE -> poll the process TREE for a
`codex` descendant whose ancestor chain includes `launcher_pid` -> record
`brain_pid`; also await readiness (a fresh `wait` heartbeat). If no `brain_pid`
by grace expiry -> kill descendants + relaunch with backoff.

Key invariant: **stop using the bus heartbeat as the SOLE liveness signal for
Codex.** `brain_pid` alive is mandatory for healthy; the heartbeat is an
additional input that distinguishes idle/busy/stuck and detects the zombie.

## New per-agent state schema

The supervisor-owned state file (`supervisor-state.json`) per agent grows from
today's `{pid, pid_alive, session_id, launched, consecutive_fails,
backoff_next_epoch, healthy_since, consumed_rids, last_warn_epoch}` to:

```jsonc
{
  "agents": {
    "codex-test": {
      // --- discovery / liveness (NEW) ---
      "launcher_pid":      19904,
      "launcher_start":    "2026-06-18T08:50:11.0Z",   // start-time, anti-reuse
      "brain_pid":         20480,                       // null until discovered
      "brain_start":       "2026-06-18T08:50:43.0Z",
      "managed_pids":      [ {"pid": 25080, "start": "...", "last_seen": 1718700750.0, "kind": "wait"},
                             {"pid": 25012, "start": "...", "last_seen": 1718700750.0, "kind": "runner"} ],
      "launching":         false,                       // in grace?
      "launch_grace_until": 0.0,                        // epoch; >now => in grace
      "last_launch_epoch": 1718700611.0,
      "readiness_seen":    true,                         // first fresh wait hb seen this launch (Q3)

      // --- resume / context ---
      // codex round-3 ruling: supervised Codex uses an ISOLATED CODEX_HOME +
      // `resume --last` (NO rollout-UUID / NO rollout diff). State only needs to
      // know whether a resumable session exists in this agent's private home.
      "codex_home":        ".agenttalk/codex-home/codex-test",  // per-agent isolated home
      "resume_available":  true,                         // reached readiness >=1x -> --last is safe

      // --- backoff / markers (UNCHANGED) ---
      "consecutive_fails": 0,
      "backoff_next_epoch": 0.0,
      "healthy_since":     1718700700.0,
      "consumed_rids":     [],
      "last_warn_epoch":   0.0
    }
  }
}
```

`launched` (today's boolean) is subsumed by `brain_pid != null || launching`.
Back-compat: a state file from the old schema is read with all new fields
defaulting (`brain_pid=null`, `launching=false`, `managed_pids=[]`,
`readiness_seen=false`); the first poll re-discovers, so no migration step is
required.

`managed_pids` freshness (codex Q6): re-derived from the snapshot EVERY poll and
persisted whenever the set changes, each row carrying `start` (anti-reuse) and
`last_seen` (epoch). A stale `managed_pids` entry is only ever used as a
cleanup/kill fallback, always start-time-guarded, never as a liveness signal.

New per-CLI config (in `supervisor.json`, defaults baked at `--init`):
`requires_brain_pid` (Codex `true`; Claude `true` until Phase 2 — see §CLI),
`brain_pattern` (process-name regex; Codex `^codex`), `launch_grace_seconds`
(default **120** — codex Q3), and `codex_home_isolation` (default **`true`** —
codex round-3 + operator decision: concurrent same-cwd Codex is NORMAL, so every
supervised Codex agent gets a private SEEDED `CODEX_HOME` and resumes with
`resume --last`; see §Resume).

## Process-snapshot input (the helper)

`build_report`/`plan_actions` cannot themselves enumerate processes (they are
pure + must stay testable). The **`.ps1` executor** captures a process snapshot
each poll and hands it to Python as part of the state (alongside the existing
`pid_alive` refresh it already does). Snapshot row shape:

```jsonc
{ "pid": 25080, "parent_pid": 20480, "name": "python.exe",
  "command_line": "...agenttalk wait --for codex-test...",
  "start_time": "2026-06-18T08:50:44.0Z" }
```

`.ps1` helper `Get-ProcSnapshot` uses `Get-CimInstance Win32_Process`
(`ProcessId`, `ParentProcessId`, `Name`, `CommandLine`, `CreationDate`).
Note from the live test: Codex's *sandboxed* `Get-CimInstance` was DENIED, but
the EXTERNAL supervisor runs unsandboxed and has permission — we design for the
real supervisor context.

**Degradation rules (codex review round 1 — BLOCKER: fail closed, NEVER to the
legacy single-PID path for a forking CLI):**
- *Partial:* if `CommandLine` is null (access-limited) but the tree is still
  enumerable, we fall back to name + ancestry only (we lose the
  `wait --for <agent>` command-line match but keep brain-discovery + tree-kill).
- *Total snapshot failure* for a CLI configured `requires_brain_pid=true`
  (Codex, and Claude until Phase 2 proves it does not fork): **FAIL CLOSED.**
  Without a snapshot we cannot find `brain_pid`, and the recorded `launcher_pid`
  is *expected* to be dead — so falling back to legacy `pid_alive` would read
  `false` and relaunch forever (the exact storm). Instead emit a new action
  `SNAPSHOT_UNAVAILABLE` -> warn + (rate-limited) operator notify, **no
  auto-kill, no auto-relaunch**, state unchanged. The supervisor effectively
  pauses managing that agent until the snapshot returns.
- *Legacy single-PID `pid_alive` fallback is ONLY permitted* for a CLI
  explicitly configured `requires_brain_pid=false` AND where
  `brain_pid == launcher_pid` has been validated (a confirmed non-forking CLI).
  This is opt-in per CLI, never the silent default.

The snapshot is **volatile and never persisted as durable state** (codex Q4):
the `.ps1` writes it to a SEPARATE `supervisor-snapshot.json` and passes
`--snapshot-file` to `supervise --plan`. It is an INPUT to the planner only; if
an inline form is ever used for speed it MUST be stripped from `next_state` and
never written back into `supervisor-state.json` (it can contain command lines).

## Classification — the state machine (6 core states + 2 guards)

Six core liveness states (LAUNCHING, HEALTHY_IDLE, ACTIVE_OR_BUSY, STUCK, DEAD,
ZOMBIE_WAIT) plus two guard states added in design-review: `READINESS_FAILED`
(round-2 MAJOR) and `SNAPSHOT_UNAVAILABLE` (round-1 BLOCKER).

Inputs per agent: `brain_alive` (brain_pid present in snapshot with matching
start-time), `hb_age`/`hb_stale` (bus heartbeat), `readiness_seen` (at least one
FRESH `wait` heartbeat has been observed SINCE this launch — codex Q3),
`wait_alive` (a managed `wait` row present), `activity_hook` (config),
`launching` (in grace), `protected`, plus the manual restart marker (unchanged,
highest priority).

**Readiness gate (codex Q2/Q3):** brain presence stops treating launcher death
as failure, but is NOT sufficient for `HEALTHY_IDLE` — that requires the FIRST
fresh `wait` heartbeat (`readiness_seen`). This makes an early `-p` exit / a
brain that never reaches the listen loop OBSERVABLE: if the brain exits, or
`readiness_seen` is never achieved, by grace expiry -> treated as a failed
launch (`DEAD`), not healthy.

The table is evaluated TOP-DOWN; the first matching row wins (so the
readiness-failed row is reached before `ACTIVE_OR_BUSY`).

| State            | Condition (first match wins, top-down)                           | Action |
|------------------|------------------------------------------------------------------|--------|
| `LAUNCHING`      | `now < launch_grace_until` (still in grace: brain not-yet-found, OR found but `!readiness_seen`) | discover brain; do NOT treat launcher death as failure; NONE |
| `READINESS_FAILED` | grace expired AND `brain_alive && !readiness_seen` (brain came up but never reached the listen loop — early `-p` exit / wedged start) | **failed launch:** kill tree + relaunch (backoff) |
| `DEAD`           | grace expired AND `!brain_alive && !wait_alive`                  | relaunch (backoff) |
| `ZOMBIE_WAIT`    | grace expired AND `!brain_alive && wait_alive`                  | kill orphan wait/runner set, THEN relaunch |
| `STUCK`          | `brain_alive && readiness_seen && hb_stale && activity_hook && stale > stuck_after` | kill tree + resume |
| `ACTIVE_OR_BUSY` | `brain_alive && readiness_seen && hb_stale && (!activity_hook OR within suspect window)` | warn/suspect only — **never kill** |
| `HEALTHY_IDLE`   | `brain_alive && readiness_seen && !hb_stale`                     | NONE |
| `SNAPSHOT_UNAVAILABLE` | total snapshot failure AND `requires_brain_pid` (evaluated first; see §Process-snapshot) | warn + operator notify, **no kill, no relaunch** |

Note the ordering closes the round-2 MAJOR: once grace has expired, a
`brain_alive && !readiness_seen` agent matches `READINESS_FAILED` (a failed
launch) and can NEVER fall through to `ACTIVE_OR_BUSY` — `ACTIVE_OR_BUSY` now
requires `readiness_seen` (it only applies to an agent that DID become ready and
later went heartbeat-stale).

Protected agents (operator-facing ∪ active leads) are NEVER auto-killed: in any
kill-worthy state they downgrade to `warn_only` (today's rule, preserved).

State machine (transitions):

```
                 launch
   (none) ────────────────▶ LAUNCHING ──brain found + fresh hb──▶ HEALTHY_IDLE
                               │  │                                   │  ▲
              grace expires,   │  │ grace expires, brain found        │  │ hb fresh
              no brain         │  └──────────────────────────────────┘  │
                               ▼                                          │ hb stale
                          DEAD/ZOMBIE ◀──── brain dies ──── HEALTHY_IDLE ─┤
                               │                                          ▼
                               │ relaunch                     ACTIVE_OR_BUSY ──hook & stale>stuck──▶ STUCK
                               └──────────────────────────────────────────────────────┘  │
                                          kill tree + resume ◀───────────────────────────┘
```

## Flows

### Launch + discovery (grace)
1. `Launch` (Codex): ensure the per-agent isolated `CODEX_HOME` is seeded (see
   §Resume bootstrap), set `CODEX_HOME` in the launch env, then run the merged
   launch layer (env apply, Quote-Arg, native exe, `Start-Process -PassThru`).
   Choose the form by `resume_available`: FRESH on the first launch
   (`codex -C <projectdir> -a never -s workspace-write $agenttalk-listen`),
   `resume --last` once a session exists. Record `launcher_pid` +
   `launcher_start`, set `launching=true`, `readiness_seen=false`,
   `launch_grace_until = now + launch_grace_seconds` (config, default **120s**
   — codex Q3).
2. Each poll while `LAUNCHING`: from the snapshot, walk children of
   `launcher_pid` (and grand-children) to find a process whose name matches the
   CLI's `brain_pattern`. When found, record `brain_pid` + `brain_start` (but
   STAY `LAUNCHING` until readiness). Set `readiness_seen=true` on the FIRST
   fresh `wait` heartbeat observed after this launch; only then is
   `HEALTHY_IDLE` reachable (codex Q2/Q3).
3. Readiness/grace failure (codex Q2): if `launch_grace_until` passes and either
   no `brain_pid` was found OR the brain was found but `readiness_seen` never
   became true (early `-p` exit / never reached the listen loop), treat as a
   FAILED launch: kill any launcher descendants, `consecutive_fails++`, relaunch
   with backoff. Launcher death DURING grace is expected and is NOT a failure.

### Classify (the table above) — pure Python in `plan_actions`.

### Kill path (executor, on STUCK / ZOMBIE_WAIT / failed-grace)
Recursive process-tree kill rooted at `brain_pid` + recorded `managed_pids` +
any matching orphan `agenttalk wait --for <agent>` (matched by command line).
**Leaves first, brain last.** Verify none remain (re-snapshot) before
relaunch. Each kill checks start-time to avoid killing a reused PID. NOT a Job
Object in v1.

### Resume (per-agent isolated CODEX_HOME + `resume --last` — codex round-3 ruling)

Codex's authoritative round-3 ruling (operator: concurrent same-cwd Codex is
NORMAL): supervised Codex agents get a **per-agent isolated, SEEDED `CODEX_HOME`
by default** and resume with **`resume --last`**. The rollout-UUID `session_id` +
rollout baseline/diff are **DROPPED from the 0.28.1 path** entirely — isolation
makes `--last` unambiguous (only this agent's sessions live in its home), so the
diff is unnecessary complexity. (Earlier rounds explored shared-home rollout
diff; superseded.)

- **Codex (DEFAULT — `codex_home_isolation=true`):** each supervised Codex agent
  runs with `CODEX_HOME=.agenttalk/codex-home/<agent>/` (a private home, NOT
  sharing `sessions/` with the operator's real home or other agents). Launch
  command: `codex -C <projectdir> resume --last -a never -s workspace-write
  $agenttalk-listen` once `resume_available` (this agent has reached readiness in
  its home at least once). **Before first readiness there is no session to
  resume — launch FRESH** (`codex -C <projectdir> -a never -s workspace-write
  $agenttalk-listen`); only after the first fresh wait heartbeat is
  `resume_available` set, so subsequent relaunches `resume --last`. Because the
  home is private, `--last` always refers to THIS agent's most recent session —
  unambiguous even when the operator runs other Codex in the same project dir.
- **Seeded-home bootstrap (codex round-3 MAJOR — required before first launch):**
  provision the isolated home with, at minimum, `auth.json`, `config.toml`, and
  `skills/` (containing at least `agenttalk-listen`), and ALSO `plugins/` +
  `rules/` when present in the real home. It MUST NOT share `sessions/` (that is
  what keeps `--last` unambiguous). Windows provisioning: **directory junctions**
  for `skills/` / `plugins/` / `rules/`; **file symlink** for `auth.json` /
  `config.toml` when symlink privilege is available, otherwise **copy with a
  restricted ACL** (and re-seed if the source changes). Validate with
  `codex doctor` — but treat it as advisory: **websocket / reachability failures
  must NOT fail the bootstrap**; only missing/invalid `auth`/`config`/skill, or no
  first fresh wait heartbeat within grace, fail closed. **Never silently fall back
  to a shared-home rollout diff** unless an operator explicitly configures it.
  Secret rules: never log `auth.json` contents; the seeded home lives under
  `.agenttalk/` (already gitignored).
- **Legacy/opt-out (non-default):** shared-home `resume --last` or the old
  rollout-diff are NOT part of the 0.28.1 default; if ever wanted they are an
  explicit, documented opt-out — not implemented in v1 unless an operator asks.
- **Claude:** already pins `--session-id <uuid>` at fresh launch and resumes
  with `--resume <uuid>` — no rollout-diff needed. (Caveat from
  claude-code-guide: session lookup is scoped to the launch directory, so keep
  `cwd` stable across relaunch.)

## `plan_actions` changes (the decision core)

`_plan_one` keeps its shape (manual marker > liveness > healthy) but the
liveness branch is rewritten from `pid_alive` to the state classifier:

- Replace `pid_alive = st.get("pid_alive")` with a `_classify(rpt, st, cfg)`
  helper returning one of the states, evaluated TOP-DOWN in the same order as
  the table (computed from `brain_alive`, `readiness_seen`, `wait_alive`,
  `hb_stale`, `activity_hook`, `launching`, grace).
- `LAUNCHING` -> `NONE` (or a new `AWAIT_GRACE` reason) — never relaunch in
  grace; set `readiness_seen=true` on the first fresh `wait` heartbeat.
- `READINESS_FAILED` (NEW — mirrors the table, evaluated BEFORE `ACTIVE_OR_BUSY`)
  -> `grace expired && brain_alive && !readiness_seen` -> `STUCK_RECOVER`-style
  `RELAUNCH` with `kill_first=tree` (the brain came up but never reached the
  listen loop — kill the wedged tree, relaunch with backoff). This is what stops
  an early-`-p` exit being mis-classified as `ACTIVE_OR_BUSY`.
- `STUCK` -> `STUCK_RECOVER` (kill_first=tree).
- `ACTIVE_OR_BUSY` (requires `readiness_seen`) -> `SUSPECT_WARN`/`NONE`
  (rate-limited) — reuses today's no-hook trap logic.
- `HEALTHY_IDLE` -> existing healthy path (advance `healthy_since`, reset
  backoff after sustained liveness).
- `DEAD` -> `RELAUNCH`.
- `ZOMBIE_WAIT` -> new `RELAUNCH` variant with `kill_orphans=true` so the
  executor reaps the orphan `wait`/runner set first.
- `SNAPSHOT_UNAVAILABLE` (NEW) -> for `requires_brain_pid` CLIs when the snapshot
  is totally absent: warn + (rate-limited) operator notify, no kill, no
  relaunch, state unchanged (the BLOCKER fix — never relaunch off a missing
  snapshot).
- `next_state` passes through the new fields (brain_pid, brain_start,
  managed_pids, launching, launch_grace_until, readiness_seen, resume_available,
  codex_home) exactly as it passes pid today (the BLOCKER-3 lesson: a healthy
  `none` tick must not drop supervisor-owned fields). The volatile snapshot is
  an INPUT only and is NEVER copied into `next_state` (codex Q4).

New action/detail fields on the plan result: `kill_orphans` (bool),
`kill_targets` (the pid+start list the executor must reap),
`discover_brain` (bool, set while LAUNCHING), `resume_mode`
(`fresh` | `last` — driven by `resume_available`).

## `.ps1` executor changes (thin)

1. `Get-ProcSnapshot` — capture the snapshot, write it to a SEPARATE
   `supervisor-snapshot.json` and pass `--snapshot-file` to `supervise --plan`
   (codex Q4: volatile, may contain command lines, must not become durable
   state). On total failure, write an explicit empty/`unavailable` marker so
   Python can choose `SNAPSHOT_UNAVAILABLE` rather than mistaking it for "no
   processes".
2. `Find-Brain $launcherPid $pattern` — ancestry walk over the snapshot (the
   script already has the snapshot; this is pure traversal).
3. `Stop-Tree $targets` — recursive, leaves-first, start-time-checked kill;
   re-snapshot to verify empty before relaunch.
4. `Seed-CodexHome $agent` — idempotently provision the per-agent isolated
   `CODEX_HOME` (junctions for `skills`/`plugins`/`rules`, symlink-or-ACL-copy for
   `auth.json`/`config.toml`, never sharing `sessions/`); advisory `codex doctor`
   (ignore websocket/reachability failures). Run before a Codex launch (§Resume).
5. The do-loop still: refresh snapshot -> `supervise --plan` -> switch on
   action. `relaunch`/`stuck_recover` now consult `kill_targets`/`kill_orphans`,
   seed the home, launch FRESH or `resume --last` per `resume_mode`, and call
   `supervise --record-launch` with the discovered `brain_pid` (+
   `resume_available=true` once readiness is seen). All of the script's own bus
   calls keep using `$AgenttalkCmd` (the merged shim).

## CLI-agnostic notes

The state machine is CLI-neutral; the per-CLI surface is a small config block —
`requires_brain_pid`, `brain_pattern`, resume form, session discovery:
1. **`requires_brain_pid`** (codex Q1) — `true` for Codex (ship it now). Claude
   stays `true` as a config knob but **destructive Claude recovery is NOT enabled
   until Phase 2** proves whether `claude.exe` forks. If Phase 2 shows Claude is
   the long-lived process itself, set its `brain_pattern` to match `claude` and
   the degenerate `brain_pid == launcher_pid` case resolves discovery
   immediately (a supported, explicit configuration — never a silent default).
2. **brain pattern** — `^codex` for codex; Claude's pattern is set in Phase 2.
3. **resume form** — codex = isolated SEEDED `CODEX_HOME` + `resume --last`
   (fresh before first readiness); claude = `--resume <pinned-uuid>` (simpler,
   already implemented — no isolated home needed since the id is pinned).
4. **session discovery** — codex = none needed (`--last` within the private home);
   claude = the uuid we minted at fresh launch.

This keeps v1 shippable for Codex without blocking on Phase 2, and ensures no
Claude agent is auto-killed/relaunched destructively before its fork behavior is
known.

Separately tracked open risk (from the memory, not this bug): **does
`claude -p /agenttalk.listen` stay alive across a long listen loop?** If `-p`
exits when the turn completes, Claude agents die after one turn — a different
cause of the same storm. The listen skill loops via repeated `agenttalk wait`
Bash calls, which may keep the turn open; UNVERIFIED. This design's brain-PID
discovery makes the *symptom* observable (brain dies -> DEAD -> relaunch), but
the real fix (if `-p` exits) belongs to Phase 2 — flagged as Q2. Do NOT switch
to `--bg` (it returns immediately and hosts the agent in Claude's daemon — an
un-monitorable launcher, exactly the handoff storm we are fixing).

## Backward compatibility

- `supervisor.json` configs are unchanged; `launch.windows_file` /
  `launch.windows_args` (the merged layer) stay. New config knobs:
  `launch_grace_seconds` (default 120), `brain_pattern`, `requires_brain_pid`,
  `codex_home_isolation` (per-CLI defaults baked at `--init`).
- `supervisor-state.json` old-schema files load with new fields defaulted;
  first poll re-discovers. No migration.
- If the snapshot is totally unavailable for a `requires_brain_pid` CLI, the
  supervisor FAILS CLOSED (`SNAPSHOT_UNAVAILABLE`): warn + operator notify, no
  kill, no relaunch (codex BLOCKER). The legacy single-PID `pid_alive` path is
  used ONLY for a CLI explicitly configured `requires_brain_pid=false` with a
  validated `brain_pid == launcher_pid` — never as a silent fallback.

## Test matrix (synthetic process snapshots — no real processes)

Decision-core tests feed `plan_actions` hand-built snapshots + state:
1. `launcher-exits-brain-lives` — launcher_pid absent, brain_pid present in
   tree -> `HEALTHY_IDLE` (NOT relaunch). The core regression for THIS bug.
2. `brain-dies-wait-lives` (ZOMBIE_WAIT) — brain absent, a `wait --for X`
   python still present -> action relaunch WITH `kill_orphans`/`kill_targets`
   listing the orphan wait+runner.
3. `brain-alive-hb-stale-with-hook` (STUCK) — brain present, hb stale >
   stuck_after, activity_hook=true -> `STUCK_RECOVER`, kill targets = tree.
4. `brain-alive-hb-stale-no-hook` (ACTIVE_OR_BUSY) — same but no hook ->
   `SUSPECT_WARN`/`NONE`, never kill.
5. `no-brain-found-in-grace` — launching, grace expired, no codex descendant ->
   relaunch + `consecutive_fails++`; AND `in-grace-launcher-dead` -> `LAUNCHING`
   (no failure) while `now < launch_grace_until`.
6. `brain-found-but-no-readiness-by-grace` (codex Q2) — brain present the whole
   grace but `readiness_seen` never true (no first wait heartbeat) -> failed
   launch -> relaunch (NOT healthy). And `brain-found-readiness-late` -> stays
   `LAUNCHING` until the first heartbeat, then `HEALTHY_IDLE`.
7. `pid-reuse` — a pid present but start-time mismatched -> treated as NOT our
   process (DEAD, not healthy).
8. `resume-mode-fresh-then-last` (codex round-3) — `resume_available=false` ->
   plan emits `resume_mode=fresh`; after readiness sets `resume_available=true`,
   a later relaunch emits `resume_mode=last`. The plan NEVER emits a rollout-UUID
   `resume <id>` (that path is dropped).
9. `seeded-home-bootstrap` (Windows-gated runtime) — `Seed-CodexHome` provisions
   a temp isolated home: junctions for `skills`/`plugins`/`rules`, file
   symlink-or-ACL-copy for `auth.json`/`config.toml`, and `sessions/` is NOT
   shared; missing auth/config/skill fails closed, a simulated websocket/doctor
   reachability failure does NOT.
10. `snapshot-unavailable-fails-closed` (codex BLOCKER) — `requires_brain_pid`
    and an `unavailable` snapshot marker -> `SNAPSHOT_UNAVAILABLE` (warn, no
    kill, no relaunch); state unchanged. AND `non-forking-cli-legacy-ok` ->
    `requires_brain_pid=false` + validated `brain==launcher` permits the legacy
    `pid_alive` path.
11. `protected-brain-dead` -> `warn_only` (never relaunch a lead).

Plus existing discipline retained: state round-trip preserves the new
supervisor-owned fields; the generated `.ps1` parses (BOM/ASCII) and its own
bus calls are PATH-independent; Windows-gated runtime tests stay gated on
`os.name=='nt'` (the `bbec73a` lesson). A NEW Windows-gated runtime test:
`Find-Brain` + `Stop-Tree` against a synthetic 2-level process tree (spawn a
`python` that spawns a child; assert tree-kill reaps both, leaves first).

## v1 vs follow-up

v1 (this work, gates 0.28.1):
- brain-PID discovery + state classifier (6 core + READINESS_FAILED /
  SNAPSHOT_UNAVAILABLE guards) (+ readiness gate + fail-closed
  `SNAPSHOT_UNAVAILABLE`) + leaves-first tree-kill + Codex resume via a per-agent
  SEEDED isolated `CODEX_HOME` + `resume --last` (rollout-UUID/diff DROPPED) + the
  snapshot helper (separate `--snapshot-file`) + the test matrix. Codex ships with
  `requires_brain_pid=true` and `codex_home_isolation=true`.

Follow-up (NOT 0.28.1):
- Windows Job Object kill containment.
- Claude `-p`-stays-alive fix + Claude `brain_pattern` once Phase 2 verifies
  whether `claude.exe` forks (destructive Claude recovery stays disabled until
  then). The readiness gate already makes an early `-p` exit observable as DEAD.
- Auto-resolution of `windows_file` (already deferred).
- `supervise --add-agent` (operator's "eventually").

## Design decisions (codex design-review round 1 — RESOLVED)

Both findings addressed and all six questions answered by codex; folded into the
sections above:

- **BLOCKER (snapshot fail-closed):** total snapshot loss for a
  `requires_brain_pid` CLI -> `SNAPSHOT_UNAVAILABLE` (warn/notify, no kill, no
  relaunch). Legacy single-PID path is opt-in only for a validated non-forking
  CLI. (§Process-snapshot, §Backward-compat, §plan_actions.)
- **MAJOR (resume ambiguity):** fail-closed rollout diff (exactly one new rollout
  matching cwd/launch-window/marker), NEVER silent `resume --last`. (The round-1
  resolution made isolated `CODEX_HOME` the default; **superseded by round-2** —
  the default is now the shared-home fail-closed diff, see below.) (§Resume.)
- **Q1:** ship Codex `requires_brain_pid=true`; Claude a config knob with
  destructive recovery disabled until Phase 2; degenerate `brain==launcher`
  supported only when explicitly configured. (§CLI-agnostic.)
- **Q2:** readiness re-check added — brain exit / no first heartbeat by grace ->
  failed launch (DEAD). (§Classification, §Launch.)
- **Q3:** grace default **120s**, configurable; `HEALTHY_IDLE` requires the
  first fresh `wait` heartbeat (not brain presence alone). (§Classification,
  §Launch.)
- **Q4:** snapshot goes to a SEPARATE `supervisor-snapshot.json` via
  `--snapshot-file`; never persisted into `next_state`. (§Process-snapshot,
  §plan_actions, §.ps1.)
- **Q5:** resume ambiguity rule (see MAJOR; superseded by round-2 default below).
- **Q6:** `managed_pids` re-derived every poll, persisted on change, with
  `start` + `last_seen`; stale entries only a start-time-guarded kill fallback.
  (§State schema.)

## Design decisions (codex design-review round 2 — RESOLVED)

- **BLOCKER (unseeded isolated home):** an empty `CODEX_HOME` loses
  auth/config/skills (codex verified `codex doctor` reports no credentials). The
  round-2 resolution made the shared-home fail-closed diff the default; **this is
  SUPERSEDED by round 3** (operator: concurrent same-cwd Codex is normal) — the
  default is now a per-agent SEEDED isolated home + `resume --last`. The empty-home
  risk is addressed by the seeding bootstrap, not by avoiding isolation.
- **MAJOR (readiness-failure conflict):** the classification table is now
  evaluated TOP-DOWN with an explicit `READINESS_FAILED` row
  (`grace expired && brain_alive && !readiness_seen` -> failed launch: kill tree
  + relaunch) placed BEFORE `ACTIVE_OR_BUSY`, and `ACTIVE_OR_BUSY` now REQUIRES
  `readiness_seen`. An early `-p` exit can no longer be mis-classified as
  warn-only. Mirrored in the `plan_actions` bullet list. (§Classification,
  §plan_actions, test 6.) **(codex round-3 confirmed this fix good.)**
- Confirmed fixed from round 1: snapshot fail-closed; no silent `resume --last`;
  Q1/Q3/Q4/Q6 folded correctly.

## Design decisions (codex design-review round 3 — RESOLVED, authoritative)

Codex's authoritative ruling (also posted to the lead) + the operator's Q5
decision settle the resume mechanism. Folded in above:

- **BLOCKER (resume mechanism):** supervised Codex defaults to a per-agent
  **isolated, SEEDED `CODEX_HOME`** (`codex_home_isolation=true`) and resumes via
  **`resume --last`**. The rollout-UUID `session_id` + rollout baseline/diff are
  **DROPPED from the 0.28.1 path** — isolation makes `--last` unambiguous. (§Resume,
  §State schema, §config knobs, v1 list.)
- **MAJOR (state/launch simplification):** state tracks `resume_available` (not
  `session_id`); launch is `codex -C <projectdir> resume --last -a never -s
  workspace-write $agenttalk-listen` once readiness has been reached, FRESH before
  that. (§State schema, §Launch, §plan_actions `resume_mode`.)
- **MAJOR (seeded-home scope):** the bootstrap provisions `auth.json`,
  `config.toml`, `skills/` (≥ `agenttalk-listen`), and `plugins/`+`rules/` when
  present; MUST NOT share `sessions/`. Windows: junctions for dir trees, file
  symlink-or-ACL-copy for files. `codex doctor` is advisory — websocket/
  reachability failures do NOT fail bootstrap; missing/invalid auth/config/skill
  or no first wait heartbeat within grace DO (fail closed). No silent shared-home
  fallback unless explicitly configured. (§Resume bootstrap, test 9.)
- **Confirmed good:** `READINESS_FAILED` / `ACTIVE_OR_BUSY` ordering closes the
  round-2 conflict.
