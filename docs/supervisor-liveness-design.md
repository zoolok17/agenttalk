# Design: supervisor liveness-model redesign (0.28.1 blocker)

Status: draft (design-review)
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
      "managed_pids":      [ {"pid": 25080, "start": "...", "kind": "wait"},
                             {"pid": 25012, "start": "...", "kind": "runner"} ],
      "launching":         false,                       // in grace?
      "launch_grace_until": 0.0,                        // epoch; >now => in grace
      "last_launch_epoch": 1718700611.0,

      // --- resume / context (NEW for codex; claude already pins) ---
      "session_id":        "0a1b...uuid",               // codex rollout UUID
      "rollout_baseline":  ["rollout-2026-...-aaaa.jsonl"], // pre-launch snapshot

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
defaulting (`brain_pid=null`, `launching=false`, `managed_pids=[]`); the first
poll re-discovers, so no migration step is required.

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
real supervisor context. **Graceful degradation:** if `CommandLine` is null
(access-limited) we fall back to name + ancestry only (we lose the
`wait --for <agent>` match but keep tree-kill); if the whole snapshot fails, we
emit an empty snapshot and Python falls back to the legacy single-PID
`pid_alive` path (warn-only, never a blind kill).

The snapshot is passed via the state file the `.ps1` already writes before
calling `supervise --plan` (add a top-level `"_snapshot": [...]` key, or a
`--snapshot-file` arg — see Open Questions Q4).

## Classification — the six states

Inputs per agent: `brain_alive` (brain_pid present in snapshot with matching
start-time), `hb_age`/`hb_stale` (bus heartbeat), `wait_alive` (a managed
`wait` row present), `activity_hook` (config), `launching` (in grace),
`protected`, plus the manual restart marker (unchanged, highest priority).

| State            | Condition                                                        | Action |
|------------------|------------------------------------------------------------------|--------|
| `LAUNCHING`      | `launching && now < launch_grace_until`                          | discover brain; do NOT treat launcher death as failure; NONE/clear on success |
| `HEALTHY_IDLE`   | `brain_alive && !hb_stale`                                       | NONE |
| `ACTIVE_OR_BUSY` | `brain_alive && hb_stale && (!activity_hook OR within suspect window)` | warn/suspect only — **never kill** |
| `STUCK`          | `brain_alive && hb_stale && activity_hook && stale > stuck_after` | kill tree + resume |
| `DEAD`           | `!brain_alive && !wait_alive` (and grace expired)                | relaunch (backoff) |
| `ZOMBIE_WAIT`    | `!brain_alive && wait_alive` (and grace expired)                 | kill orphan wait/runner set, THEN relaunch |

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
   `launching=true`, `launch_grace_until = now + grace_seconds` (config,
   default 90s), snapshot `rollout_baseline` (codex; see Resume).
2. Each poll while `LAUNCHING`: from the snapshot, walk children of
   `launcher_pid` (and grand-children) to find a process whose name matches the
   CLI's brain pattern (`codex` for codex; TBD for claude — Q1). When found,
   record `brain_pid` + `brain_start`, set `launching=false`. Also require a
   *fresh* `wait` heartbeat as readiness before declaring `HEALTHY_IDLE`.
3. If `launch_grace_until` passes with no brain: kill any launcher descendants,
   `consecutive_fails++`, relaunch with backoff. Launcher death DURING grace is
   expected and is NOT counted as a failure.

### Classify (the table above) — pure Python in `plan_actions`.

### Kill path (executor, on STUCK / ZOMBIE_WAIT / failed-grace)
Recursive process-tree kill rooted at `brain_pid` + recorded `managed_pids` +
any matching orphan `agenttalk wait --for <agent>` (matched by command line).
**Leaves first, brain last.** Verify none remain (re-snapshot) before
relaunch. Each kill checks start-time to avoid killing a reused PID. NOT a Job
Object in v1.

### Resume (deterministic context pin — FULL scope)
- **Codex:** snapshot `.codex/sessions/**/rollout-*.jsonl` BEFORE launch
  (`rollout_baseline`) and again after the brain is ready; the NEW rollout UUID
  for this agent/cwd is the agent's `session_id`. Relaunch as
  `resume <session_id> -a never -s workspace-write $agenttalk-listen`
  (deterministic — NOT `resume --last`, which is ambiguous when two codex share
  a cwd). Documented fallback: an isolated `CODEX_HOME` per agent so the rollout
  set is unambiguous.
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
- `next_state` passes through the new fields (brain_pid, managed_pids,
  session_id, launching, grace) exactly as it passes pid/session_id today (the
  BLOCKER-3 lesson: a healthy `none` tick must not drop supervisor-owned
  fields).

New action/detail fields on the plan result: `kill_orphans` (bool),
`kill_targets` (the pid+start list the executor must reap),
`discover_brain` (bool, set while LAUNCHING), `resume_session_id`.

## `.ps1` executor changes (thin)

1. `Get-ProcSnapshot` — capture the snapshot, write it into the state the
   script already passes to `supervise --plan` (or `--snapshot-file`).
2. `Find-Brain $launcherPid $pattern` — ancestry walk over the snapshot (the
   script already has the snapshot; this is pure traversal).
3. `Stop-Tree $targets` — recursive, leaves-first, start-time-checked kill;
   re-snapshot to verify empty before relaunch.
4. `Snapshot-Rollouts $cwd` — list `.codex/sessions/**/rollout-*.jsonl` for the
   baseline/diff (codex only).
5. The do-loop still: refresh snapshot -> `supervise --plan` -> switch on
   action. `relaunch`/`stuck_recover` now consult `kill_targets`/`kill_orphans`
   and call `supervise --record-launch` with the discovered `brain_pid` +
   `session_id`. All of the script's own bus calls keep using `$AgenttalkCmd`
   (the merged shim).

## CLI-agnostic notes

The state machine is CLI-neutral; only three things are per-CLI:
1. **brain pattern** — `codex` process name for codex; **OPEN for claude (Q1)**
   — Claude may NOT fork (Phase 2 unverified). If `claude.exe` is itself the
   long-lived process, `brain_pid == launcher_pid` and discovery resolves
   immediately (a degenerate, supported case).
2. **resume form** — codex = rollout-UUID `resume <id>`; claude = `--resume
   <pinned-uuid>` (simpler, already implemented).
3. **session discovery** — codex = rollout snapshot diff; claude = the uuid we
   minted at fresh launch (no discovery needed).

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
  `launch_grace_seconds` (default 90), `brain_pattern` (per-CLI default).
- `supervisor-state.json` old-schema files load with new fields defaulted;
  first poll re-discovers. No migration.
- If the snapshot is empty/denied, Python degrades to the legacy single-PID
  `pid_alive` path (warn-only on staleness, no blind kill).

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
   relaunch + `consecutive_fails++`; AND `in-grace-launcher-dead` -> NONE (no
   failure) while `now < launch_grace_until`.
6. `pid-reuse` — a pid present but start-time mismatched -> treated as NOT our
   process (DEAD, not healthy).
7. `deterministic-session-pin` — rollout baseline vs post-launch diff yields
   exactly one new UUID -> stored as session_id; resume command uses
   `resume <id>` not `--last`.
8. `protected-brain-dead` -> `warn_only` (never relaunch a lead).

Plus existing discipline retained: state round-trip preserves the new
supervisor-owned fields; the generated `.ps1` parses (BOM/ASCII) and its own
bus calls are PATH-independent; Windows-gated runtime tests stay gated on
`os.name=='nt'` (the `bbec73a` lesson). A NEW Windows-gated runtime test:
`Find-Brain` + `Stop-Tree` against a synthetic 2-level process tree (spawn a
`python` that spawns a child; assert tree-kill reaps both, leaves first).

## v1 vs follow-up

v1 (this work, gates 0.28.1):
- brain-PID discovery + 6-state classifier + tree-kill + deterministic codex
  resume + the snapshot helper with graceful degradation + the test matrix.

Follow-up (NOT 0.28.1):
- Windows Job Object kill containment.
- Claude `-p`-stays-alive fix if Phase 2 shows it exits (Q2).
- Auto-resolution of `windows_file` (already deferred).
- `supervise --add-agent` (operator's "eventually").

## Open questions for codex (design-review)

- **Q1 (brain pattern for claude):** does `claude.exe` fork like `codex.exe`,
  or is it the long-lived process itself? If unknown until Phase 2, is the
  "degenerate: brain==launcher" handling sufficient to ship v1 for codex while
  leaving claude's pattern a config knob?
- **Q2 (`-p` longevity):** out of scope for this bug, but do you want the
  design to ASSUME the listen loop keeps the turn open, or to add a readiness
  re-check that catches an early `-p` exit as DEAD?
- **Q3 (grace window):** 90s default reasonable for codex TUI cold start on the
  operator's box, or longer? Should readiness require the FIRST `wait`
  heartbeat, or just brain presence?
- **Q4 (snapshot transport):** inline `_snapshot` key in the state file the
  `.ps1` already writes, vs a separate `--snapshot-file`? Inline is simplest
  (one read) but bloats the state file; a separate file keeps state clean.
- **Q5 (rollout diff reliability):** is the pre/post `rollout-*.jsonl` diff
  robust when the operator runs other codex sessions in the same `cwd`
  concurrently, or should isolated `CODEX_HOME` be the DEFAULT rather than the
  fallback?
- **Q6 (managed_pids freshness):** persist the full last-seen descendant set
  every poll, or only refresh on state change? Persisting every poll defeats
  pid-reuse better but writes more.
