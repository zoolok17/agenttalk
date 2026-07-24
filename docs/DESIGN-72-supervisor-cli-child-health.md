# Design #72: verify the wrapped CLI child in supervisor health

Status: proposed, diagnosis and design only
Date: 2026-07-24
Base: `c70ee918c8e060191e125b12582dc9dcbcb23b18`
Audience: supervisor and wrapper maintainers

## Decision summary

`HEALTHY_IDLE` must mean all of the following:

1. the managed wrapper instance is live;
2. the wrapper reports that no CLI turn is active; and
3. the wrapper heartbeat is fresh.

During an active turn, health must instead require an independently observed
live CLI brain and recent CLI progress. A fresh wrapper heartbeat alone is
never enough. Missing, malformed, stale, or unqueryable child evidence is not
green. It is also not kill authority.

The recommended design combines:

- a strict, atomic wrapper-runtime observation that identifies the active turn,
  the direct CLI launcher, and monotonic progress;
- the existing process snapshot and launcher-ascent logic to find the real CLI
  brain; and
- the existing supervisor backoff and restart path after death or a progress
  stall is confirmed.

This design deliberately distinguishes an idle wrapper, for which no CLI child
is expected, from an active turn whose CLI child is required.

## Diagnosis

### Current verdict path

The operator's core claim is confirmed at the classifier boundary.

1. `build_report` derives `heartbeat_stale` only from heartbeat age and the
   resolved threshold (`supervisor.py:3228-3258`). It reads wrapper health, but
   that health remains advisory and does not participate in the liveness
   verdict.
2. `plan_actions` calls `_liveness` and then `_plan_one`
   (`supervisor.py:4361-4399`).
3. `_liveness` explicitly short-circuits wrapped agents
   (`supervisor.py:3123-3133`). It returns `brain_pid=None`,
   `brain_alive=False`, and `discovered_brain=False`, even when the snapshot
   contains a CLI child. The snapshot is retained only for attributed cleanup
   targets.
4. `_plan_one` reads the liveness data for carry-forward and kill targets, but
   does not gate the verdict on `brain_alive`. After handling stale-heartbeat
   cases, every fresh heartbeat reaches the unconditional
   `HEALTHY_IDLE` return (`supervisor.py:4157-4162`).
5. `project_coordination_availability` has the same false-green edge: after
   validating the heartbeat field, a fresh heartbeat immediately returns
   `AVAILABLE / heartbeat_fresh` (`supervisor.py:715-730`).

Therefore a live wrapper plus a fresh heartbeat can be classified green while
an active turn has no live CLI brain. Neither the planner nor coordination
availability currently requires the child.

### Important operational qualification

The statement that the wrapper always keeps the heartbeat fresh regardless of
child liveness is too broad:

- a recognized failed turn clears its heartbeat;
- the Claude work-heartbeat ticker checks the direct `Popen` child and stops
  after a configured cap; and
- streaming adapter progress stamps the heartbeat.

However, those mechanisms do not repair the classifier:

- idle polling stamps a wrapper-only heartbeat;
- the bounded work-heartbeat is child-liveness, not model progress;
- dead-letter disposition intentionally stamps a fresh heartbeat and calls the
  disposition "progress" (`wrapper/loop.py:404-409`); and
- the supervisor never checks the wrapped child before returning
  `HEALTHY_IDLE`.

The current behavior can therefore conceal an active-child failure during an
observation window, a live-but-wedged child until the heartbeat cap or stale
threshold, and the operational result after repeated failures are
dead-lettered.

### Signals that already exist

The wrapper already emits most of the semantic transitions needed:

- `WrapperHealthWriter.turn_start` writes `working_silent`;
- adapter progress writes `working_turn` and `last_progress_at`;
- completion writes `idle_waiting`;
- the attempt ledger marks `in_progress` before `drive`; and
- the cursor advances on success, control disposition, or dead-letter.

These are useful inputs, but no one existing signal is sufficient:

- `health.json` is explicitly advisory, has no child locator or monotonic
  progress generation, and may become `unknown` when a newer synthetic
  heartbeat invalidates it;
- an `in_progress` attempt proves an active turn, but not child liveness or
  progress;
- cursor stability cannot distinguish idle from wedged; and
- cursor advance treats dead-letter and successful completion alike.

## Forking CLI reality

The existing liveness defaults encode the required distinction
(`supervisor.py:352-359`):

- Codex has `allow_launcher_self=False`. Its `codex.exe` launcher can spawn the
  real TUI/brain and exit. The launcher PID is not the brain.
- Claude has `allow_launcher_self=True`. The launched `claude.exe` can itself
  be the brain.

`_discover_brain`, `_depth_to_launcher`, and `_pid_alive_guarded`
(`supervisor.py:1923-1930,2918-3037`) already implement the correct start-time
guarded ascent for the non-wrapped path. The wrapped path bypasses it.

For a wrapped turn, the Python wrapper is the durable outer root, while the
per-turn `Popen` PID is the CLI launcher. Brain discovery must use that
per-turn launcher PID and start token as a locator:

- for Claude, the guarded launcher row is the brain;
- for Codex, the launcher is excluded and ascent selects a live matching
  descendant, including the TUI after the launcher exits.

Using only the outer Python wrapper as the ascent anchor is insufficient for
Codex after the intermediate launcher exits. The wrapper must publish the
per-turn launcher locator. The process snapshot remains the authority for the
observed path, identity, ancestry, and current liveness.

If ancestry is missing or ambiguous on Windows or POSIX, the result is
`unknown`, never green and never automatic kill authority.

## Options considered

### Option A: require a matching process name under every wrapped process

Reject. An idle wrapper legitimately has no CLI child. Name-only matching also
misidentifies the short-lived Codex launcher and is unsafe under PID reuse.

### Option B: make the existing advisory health snapshot authoritative

Reject as-is. It lacks wrapper-instance binding, a child locator, and monotonic
progress. Its freshness rules are coupled to the heartbeat it is meant to
cross-check. Promoting it directly would turn missing or delayed advisory
writes into ambiguous control behavior.

### Option C: strict runtime observation plus independent process discovery

Recommend. Add a small, closed-schema runtime record and use it only with the
existing process snapshot. This separates three facts that the heartbeat
currently conflates:

- wrapper lifecycle;
- active CLI-brain lifecycle; and
- CLI turn progress.

## Recommended mechanism

### 1. Wrapper-runtime observation

The wrapper atomically writes `state/<agent>.wrapper-runtime.json` with a closed
schema. Suggested fields:

```json
{
  "schema_version": 1,
  "agent": "worker",
  "wrapper_pid": 4100,
  "wrapper_start": "2026-07-24T15:00:00.000000Z",
  "wrapper_generation": "bounded-token",
  "phase": "idle|starting|active|terminal",
  "turn_generation": 17,
  "turn_id": "bounded-token",
  "message_id": "20260724-...",
  "cli_launcher_pid": 4200,
  "cli_launcher_start": "2026-07-24T15:03:00.000000Z",
  "progress_sequence": 9,
  "last_progress_at": "2026-07-24T15:03:08.000000Z",
  "last_outcome": "success|failed|dead_letter|null",
  "updated_at": "2026-07-24T15:03:08.000000Z"
}
```

The record contains no prompt, model output, command, or tool output. Writes
use same-directory temp, flush, and replace. Readers reject unknown keys,
invalid types, unsafe identifiers, non-UTC/future timestamps, and inconsistent
phase fields.

The wrapper writes:

1. `starting` immediately before spawn;
2. `active` immediately after `Popen` returns, with the launcher locator;
3. a higher `progress_sequence` on real adapter progress;
4. `terminal` with a bounded outcome when the child ends; and
5. `idle` only after the loop is ready to receive another record.

Progress events may be coalesced to one atomic write per bounded interval. The
in-memory sequence still advances for every accepted event, and the terminal
write is forced, so coalescing cannot hide a completed or failed boundary.

The wrapper PID/start and generation bind observations to the currently
managed wrapper instance. They are consistency controls, not authentication.
The supervisor independently validates the wrapper row before using the
record.

### 2. CLI-brain observation

Extend wrapped `_liveness` rather than creating a second process model:

1. validate the wrapper instance with the existing start-time and invocation
   checks;
2. read the runtime record strictly;
3. when `phase` is active, validate the per-turn launcher PID/start;
4. run the existing ascent/discovery algorithm with CLI-specific
   `allow_launcher_self`; and
5. return a tri-state child observation: `alive`, `dead`, or `unknown`.

`dead` requires an available snapshot and a positively identified active-turn
lineage that is now absent. Snapshot failure, an unparseable start token, a
runtime-record mismatch, or ancestry that cannot be established by the
existing command-line or name-plus-parent fallback yields `unknown`.

### 3. Progress observation

Heartbeat refresh is not progress. The progress signal is a monotonic
`progress_sequence` advanced only by:

- model output/delta events;
- tool start/finish events emitted by the CLI adapter; or
- a terminal turn boundary.

Turn start, wrapper polling, work-heartbeat ticks, and cursor advance alone do
not advance it. A dead-letter is recorded as an outcome, not converted into a
successful progress signal.

The supervisor stores the last observed wrapper generation, turn generation,
progress sequence, and the local epoch when each changed. Using sequence
changes avoids relying solely on writer clock synchronization. A new wrapper
or turn resets the comparison baseline.

### 4. Verdict table

Evaluate these rows before the existing fresh-heartbeat branch:

| Runtime phase | CLI brain | Progress | Verdict | Automatic action |
| --- | --- | --- | --- | --- |
| valid idle | not required | n/a | `HEALTHY_IDLE` | none |
| starting, inside spawn grace | not yet known | n/a | `CLI_CHILD_STARTING` | none |
| active | alive | recent | `HEALTHY_WORKING` | none |
| active | dead, first observation | n/a | `CLI_CHILD_MISSING` | none |
| active | dead, confirmed | n/a | `CLI_CHILD_DEAD` | existing restart path |
| active | unknown | any | `CLI_CHILD_UNKNOWN` | warn, no kill |
| active | alive | stale | `CLI_CHILD_STALLED` | existing stuck path after threshold |
| terminal success/finalizing | not required | recent | `HEALTHY_WORKING` | none |
| terminal failure/dead-letter | not required | n/a | `TURN_FAILED` | wrapper policy/notify |
| missing/invalid runtime record | any | any | `CLI_CHILD_UNKNOWN` | warn, no kill |

`HEALTHY_IDLE` and `HEALTHY_WORKING` are the only green wrapped states.
Coordination availability must inspect these states before accepting a fresh
heartbeat.

### 5. Death, wedge, and restart policy

Child death and child stall are different failures:

- **Dead:** the active-turn brain is absent from a trustworthy snapshot.
  Confirm across two polls for the same wrapper/turn generation, outside a
  short spawn/handoff grace. Then use the existing `STUCK_RECOVER` executor
  path, start-guarded kill targets, exponential backoff, and readiness cap.
- **Wedged:** the brain is alive but `progress_sequence` has not advanced past
  the per-CLI turn-progress threshold. Preserve the current Codex watchdog
  ordering: the supervisor must not preempt the per-turn watchdog before its
  deadline plus margin. After confirmation, use the same restart path with a
  distinct reason.
- **Unknown:** the evidence cannot establish death or health. It is not green,
  but it must not authorize a kill or restart. Emit a rate-limited warning and
  retry observation.

Debouncing on wrapper generation, turn generation, and two consecutive polls
prevents restart thrash during spawn and Codex launcher handoff. Existing
`backoff_next_epoch`, `consecutive_fails`, and readiness retry caps remain the
restart controls. A child-health failure must not reset them merely because
the wrapper heartbeat stayed fresh.

The terminal observation also closes the normal-exit race: a child that exits
after emitting its terminal boundary is finalizing, not dead. Only an
`active` observation with no live brain is eligible for the missing/dead
sequence.

Infrastructure child death or supervisor recovery must remain an
infrastructure/ambiguous attempt outcome, not become low-threshold message
poison. Otherwise recovery itself could accelerate dead-lettering.

### 6. Rollout and compatibility

The supervisor and wrapper-runtime writer must ship together. After script
refresh, an older still-running wrapper has no runtime record; it therefore
reports `CLI_CHILD_UNKNOWN`, with no kill or relaunch, and gives an exact
restart/refresh remediation. It must not silently fall back to heartbeat-only
green. The first `idle` observation from the refreshed wrapper establishes the
new baseline.

## Failing regression

`test_wrapped_active_turn_with_dead_cli_brain_is_not_healthy_idle` builds:

- a fresh wrapper heartbeat;
- a current `working_turn` observation with recent real progress;
- a live, correctly attributed Python wrapper in the process snapshot; and
- no Codex launcher or brain row.

Current behavior returns `HEALTHY_IDLE`. The test asserts that an active turn
with no CLI brain must not be `HEALTHY_IDLE`, so it fails on the base revision.
It intentionally does not prescribe a kill on the first observation; both
`CLI_CHILD_MISSING` and `CLI_CHILD_UNKNOWN` satisfy the fail-safe requirement.

## Implementation test matrix

The implementation round should add deterministic synthetic-snapshot tests for:

- idle wrapper without a child remains `HEALTHY_IDLE`;
- active Claude launcher/self is `HEALTHY_WORKING`;
- active Codex launcher exit plus live TUI grandchild is discovered correctly;
- active turn plus confirmed absent child becomes `CLI_CHILD_DEAD`;
- snapshot/ancestry/start-token ambiguity becomes `CLI_CHILD_UNKNOWN`, never
  green and never kill;
- a progressing child stays working;
- a live child with an unchanged progress sequence becomes stalled only after
  the configured threshold and confirmation poll;
- work-heartbeat ticks do not count as progress;
- wrapper/turn generation changes reset the stall/death debounce;
- restart backoff and readiness caps still prevent relaunch storms;
- dead-letter is visible as a terminal outcome and is not reported as a
  successful turn; and
- the availability view cannot return `AVAILABLE` for child dead, unknown, or
  stalled states even with a fresh heartbeat.

Windows and POSIX tests should use the shared process-snapshot row contract.
Platform-specific snapshot adapters need focused tests, while the verdict
table remains pure and platform-independent.

## Residual risks and boundaries

- Process presence cannot prove model progress; that is why the runtime
  sequence is required.
- Process ancestry can be lost after reparenting. The design fails safe to
  unknown instead of guessing.
- A same-user process can rewrite consistency files. This design prevents
  accidental false-green states; it is not an authentication boundary.
- A terminal child failure can occur between polls. The wrapper's terminal
  observation remains necessary even when the supervisor never sees the dead
  process.
- This change should surface dead-letter outcomes, but it does not redesign
  dead-letter policy or automatically requeue disposed work.
