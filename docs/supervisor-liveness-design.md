# Design: supervisor liveness-model redesign (0.28.1 blocker)

Status: draft (design-review round 2 — codex round-1 findings folded in)
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
one of six states. The decision core stays pure Python (`plan_actions`); the
`.ps1` stays a thin executor that supplies a process snapshot and performs
kills. The merged launch layer (`launch.windows_file`/`windows_args`) is kept
as-is; this work **adds** state fields + discovery rather than ripping it out.

## Goals / non-goals

Goals:
- Never relaunch an agent that is actually alive (kill the storm).
- Never be fooled into "healthy" by an orphaned `wait` heartbeat (kill the
  zombie).
- Preserve context across a supervisor-driven relaunch (deterministic
  session pin — operator approved FULL scope).
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
- `session_id` — the Codex rollout UUID, discovered once, for deterministic
  resume.
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
      "readiness_seen":    true,                         // first fresh wait hb seen post-launch (Q3)

      // --- resume / context (NEW for codex; claude already pins) ---
      "session_id":        "0a1b...uuid",               // codex rollout UUID (deterministic)
      "rollout_baseline":  ["rollout-2026-...-aaaa.jsonl"], // pre-launch snapshot for the diff

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
(default **120** — codex Q3), and `codex_home_isolation` (default `true` for
supervised Codex — see §Resume).

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

## Classification — the six states

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

| State            | Condition                                                        | Action |
|------------------|------------------------------------------------------------------|--------|
| `LAUNCHING`      | `launching && now < launch_grace_until` (brain not-yet-found, OR found but `!readiness_seen`) | discover brain; do NOT treat launcher death as failure; NONE |
| `HEALTHY_IDLE`   | `brain_alive && readiness_seen && !hb_stale`                     | NONE |
| `ACTIVE_OR_BUSY` | `brain_alive && (hb_stale OR !readiness_seen) && (!activity_hook OR within suspect window)` | warn/suspect only — **never kill** |
| `STUCK`          | `brain_alive && readiness_seen && hb_stale && activity_hook && stale > stuck_after` | kill tree + resume |
| `DEAD`           | grace expired AND `!brain_alive && !wait_alive` (incl. brain never found / exited before readiness) | relaunch (backoff) |
| `ZOMBIE_WAIT`    | grace expired AND `!brain_alive && wait_alive`                  | kill orphan wait/runner set, THEN relaunch |
| `SNAPSHOT_UNAVAILABLE` | total snapshot failure AND `requires_brain_pid` (see §Process-snapshot) | warn + operator notify, **no kill, no relaunch** |

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
1. `Launch` runs the merged launch layer (env apply, Quote-Arg, native exe,
   `Start-Process -PassThru`). Record `launcher_pid` + `launcher_start`, set
   `launching=true`, `readiness_seen=false`,
   `launch_grace_until = now + launch_grace_seconds` (config, default **120s**
   — codex Q3), snapshot `rollout_baseline` (codex; see Resume).
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

### Resume (deterministic context pin — FULL scope)

codex review round 1 — MAJOR: deterministic resume needs a FIRM ambiguity rule;
never silently fall back to `resume --last`. Resolution: make per-agent
**`CODEX_HOME` isolation the DEFAULT** for supervised Codex, with the rollout
diff as a fail-closed secondary check.

- **Codex (default — `codex_home_isolation=true`):** the supervisor launches the
  agent with a dedicated `CODEX_HOME` (e.g. `.agenttalk/codex-home/<agent>/`), so
  that agent's `sessions/**/rollout-*.jsonl` set is unambiguous and isolated from
  the operator's other Codex sessions. The single rollout in that home is the
  agent's `session_id`; relaunch as
  `resume <session_id> -a never -s workspace-write $agenttalk-listen`.
- **Codex (rollout diff, when isolation is disabled):** snapshot
  `.codex/sessions/**/rollout-*.jsonl` before launch (`rollout_baseline`) and
  after the brain is ready. **FAIL CLOSED:** accept a `session_id` ONLY if
  EXACTLY ONE new rollout matches this agent's cwd + launch window
  (`mtime ∈ [launch, ready]`) + agent marker. If zero or >1 match, do NOT claim
  a deterministic `session_id` and **never** silently use `resume --last`:
  relaunch FRESH (`$agenttalk-listen`, new context) and warn that context was not
  preserved this cycle. Ambiguity downgrades context preservation; it never
  resumes the wrong session.
- **Claude:** already pins `--session-id <uuid>` at fresh launch and resumes
  with `--resume <uuid>` — no rollout-diff needed. (Caveat from
  claude-code-guide: session lookup is scoped to the launch directory, so keep
  `cwd` stable across relaunch.)

## `plan_actions` changes (the decision core)

`_plan_one` keeps its shape (manual marker > liveness > healthy) but the
liveness branch is rewritten from `pid_alive` to the six-state classifier:

- Replace `pid_alive = st.get("pid_alive")` with a `_classify(rpt, st, cfg)`
  helper returning one of the six states (computed from `brain_alive`,
  `wait_alive`, `hb_stale`, `activity_hook`, `launching`, grace).
- `LAUNCHING` -> `NONE` (or a new `AWAIT_GRACE` reason) — never relaunch in
  grace.
- `HEALTHY_IDLE` -> existing healthy path (advance `healthy_since`, reset
  backoff after sustained liveness).
- `ACTIVE_OR_BUSY` -> `SUSPECT_WARN`/`NONE` (rate-limited) — reuses today's
  no-hook trap logic.
- `STUCK` -> `STUCK_RECOVER` (kill_first=tree).
- `DEAD` -> `RELAUNCH`.
- `ZOMBIE_WAIT` -> new `RELAUNCH` variant with `kill_orphans=true` so the
  executor reaps the orphan `wait`/runner set first.
- `SNAPSHOT_UNAVAILABLE` (NEW) -> for `requires_brain_pid` CLIs when the snapshot
  is totally absent: warn + (rate-limited) operator notify, no kill, no
  relaunch, state unchanged (the BLOCKER fix — never relaunch off a missing
  snapshot).
- `next_state` passes through the new fields (brain_pid, brain_start,
  managed_pids, session_id, launching, launch_grace_until, readiness_seen)
  exactly as it passes pid/session_id today (the BLOCKER-3 lesson: a healthy
  `none` tick must not drop supervisor-owned fields). The volatile snapshot is
  an INPUT only and is NEVER copied into `next_state` (codex Q4).

New action/detail fields on the plan result: `kill_orphans` (bool),
`kill_targets` (the pid+start list the executor must reap),
`discover_brain` (bool, set while LAUNCHING), `resume_session_id`.

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
4. `Snapshot-Rollouts $codexHome` — list `<CODEX_HOME>/sessions/**/rollout-*.jsonl`
   for the baseline/diff (codex only; defaults to the per-agent isolated
   `CODEX_HOME` so the set is unambiguous — see §Resume).
5. The do-loop still: refresh snapshot -> `supervise --plan` -> switch on
   action. `relaunch`/`stuck_recover` now consult `kill_targets`/`kill_orphans`
   and call `supervise --record-launch` with the discovered `brain_pid` +
   `session_id`. All of the script's own bus calls keep using `$AgenttalkCmd`
   (the merged shim).

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
3. **resume form** — codex = isolated-`CODEX_HOME` (default) or fail-closed
   rollout-UUID `resume <id>`; claude = `--resume <pinned-uuid>` (simpler,
   already implemented).
4. **session discovery** — codex = the single rollout in its isolated home (or
   fail-closed diff); claude = the uuid we minted at fresh launch (none needed).

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
8. `deterministic-session-pin` — isolated `CODEX_HOME` with exactly one rollout
   -> stored as session_id; resume uses `resume <id>` not `--last`.
9. `rollout-ambiguous-fails-closed` (codex MAJOR) — isolation disabled and the
   diff finds 0 or >1 matching new rollouts -> NO `session_id`, relaunch FRESH +
   warn; the plan NEVER emits `resume --last`.
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
- brain-PID discovery + 6-state classifier (+ readiness gate + fail-closed
  `SNAPSHOT_UNAVAILABLE`) + leaves-first tree-kill + deterministic Codex resume
  (isolated `CODEX_HOME` default, fail-closed rollout diff) + the snapshot helper
  (separate `--snapshot-file`) + the test matrix. Codex ships with
  `requires_brain_pid=true`.

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
- **MAJOR (resume ambiguity):** isolated `CODEX_HOME` is the DEFAULT for
  supervised Codex; the rollout diff is fail-closed (exactly one new rollout
  matching cwd/launch-window/marker) and NEVER silently uses `resume --last`.
  (§Resume.)
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
- **Q5:** see MAJOR above (isolated `CODEX_HOME` default).
- **Q6:** `managed_pids` re-derived every poll, persisted on change, with
  `start` + `last_seen`; stale entries only a start-time-guarded kill fallback.
  (§State schema.)
