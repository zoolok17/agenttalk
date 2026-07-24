# Design #72: verify the wrapped CLI child in supervisor health

Status: implemented on `feat/72-supervisor-cli-child-health`
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

The wrapped-health classifier is total. Every accepted combination of runtime
phase, brain observation, and progress observation resolves inside the new
classifier. Only a strictly validated `idle` phase may produce
`HEALTHY_IDLE`; no non-idle or unenumerated combination may fall through to
the legacy fresh-heartbeat branch.

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
use a same-directory temporary file, flush and `fsync` the file, then replace
the destination. The forced terminal write is never coalesced and is also
`fsync`ed before replacement; implementations should additionally `fsync` the
directory where the platform supports it. The existing #32 sandbox-direct
latch is not a persistence model for this record because it is not
crash-atomic.

Readers consume the whole file and validate it against the closed schema
before exposing any field. They reject unknown keys, invalid types, unsafe
identifiers, non-UTC timestamps, timestamps beyond the bounded supervisor
observation skew, and inconsistent phase fields. A timestamp within that bound
is age-clamped to zero because the wrapper can publish after the generated
supervisor captures integer `--now` but before Python reads the file. An absent,
torn, partial, or parse-failed read becomes one indivisible
`CLI_CHILD_UNKNOWN` observation. A reader must never salvage `phase`,
`progress_sequence`, or an outcome from invalid bytes.

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

#### Coherence with #73

`wrapper-runtime.json` is the #72 health authority for wrapper/CLI liveness,
progress, and restart decisions. The wrapper is its sole writer. It is a
self-report and never authorizes a bus commit, cursor advance, or thread-seen
decision.

#73 owns commit-vs-park independently through the validated bus. It does not
read `last_outcome`, `phase`, or `progress_sequence` from this health record.
The two authorities must be coherent rather than unified: a #72 recovery may
replace a dead child or wrapper, but it must not rewind bus cursor/thread-seen
state or re-drive work that #73 already committed. A replacement wrapper
starts from the already-advanced cursor and receives only later inbound work.
A post-publish child crash can therefore be both work-landed under #73 and
child-dead under #72; restart is correct only when it preserves that committed
bus boundary.

The schema, parser, validation rules, and atomic writer live behind the #72
wrapper-health boundary. Supervisor debounce state is a cache of health
observations, not another authority and not a commit ledger.

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

A sequence decrease is evaluated only after a complete, valid record is bound
to the same wrapper and turn generations. It is then treated as invalid or
ambiguous evidence and resolves to `CLI_CHILD_UNKNOWN`; it never constitutes
proof of death or a stall. Partial reads cannot create a synthetic phase or
sequence regression.

### 4. Verdict table

The wrapped classifier returns from this table and never falls through to the
legacy fresh-heartbeat classifier. It is deliberately total:

| Runtime phase | CLI brain | Progress | Verdict | Automatic action |
| --- | --- | --- | --- | --- |
| valid idle, live wrapper, fresh heartbeat | not required | n/a | `HEALTHY_IDLE` | none |
| valid idle, wrapper or heartbeat not green | not required | n/a | existing non-green wrapper verdict | existing wrapper path |
| starting, inside spawn grace | not yet known | n/a | `CLI_CHILD_STARTING` | none |
| active | alive | recent | `HEALTHY_WORKING` | none |
| active | dead, first observation | n/a | `CLI_CHILD_MISSING` | none |
| active | dead, confirmed | n/a | `CLI_CHILD_DEAD` | existing restart path |
| active | unknown | any | `CLI_CHILD_UNKNOWN` | warn, no kill |
| active | alive | stale | `CLI_CHILD_STALLED` | existing stuck path after threshold |
| terminal success/finalizing | not required | recent | `HEALTHY_WORKING` | none |
| terminal failure/dead-letter | not required | n/a | `TURN_FAILED` | wrapper policy/notify |
| terminal | any | stale or unclassified | `CLI_CHILD_UNKNOWN` | warn, no kill |
| any valid starting/active/terminal combination not matched above | any | any | `CLI_CHILD_UNKNOWN` | warn, no kill |
| missing/invalid/torn/partial/parse-failed runtime record | any | any | `CLI_CHILD_UNKNOWN` | warn, no kill |

`HEALTHY_IDLE` and `HEALTHY_WORKING` are the only green wrapped states.
Coordination availability must inspect these states before accepting a fresh
heartbeat. The idle rows explicitly preserve existing non-green wrapper
recovery while making strict idle validation a precondition for the legacy
heartbeat decision. The default row for every unenumerated tuple is
`CLI_CHILD_UNKNOWN`. In particular, a fresh heartbeat cannot turn a terminal
record with stale progress, an unknown phase, or an otherwise unmatched tuple
green. Only the first row reaches `HEALTHY_IDLE`.

The implementation order is normative:

1. strictly parse and bind the complete runtime record; on any failure, return
   `CLI_CHILD_UNKNOWN`;
2. for `idle`, run the existing wrapper/heartbeat sub-classifier and return its
   result;
3. for `starting`, `active`, or `terminal`, return a matching row above; and
4. otherwise return `CLI_CHILD_UNKNOWN`.

There is no continuation from steps 3 or 4 into the legacy fresh-heartbeat
branch.

### 5. Death, wedge, and restart policy

Child death and child stall are different failures:

- **Dead:** the active-turn brain is absent from a trustworthy snapshot.
  Confirm across two polls for the same wrapper/turn generation, outside a
  short spawn/handoff grace. Then use the existing `STUCK_RECOVER` executor
  path, start-guarded kill targets, exponential backoff, and readiness cap.
- **Wedged:** the brain is alive but `progress_sequence` has not advanced past
  the per-CLI turn-progress threshold. The stall threshold has a hard
  invariant: it must be greater than or equal to the resolved per-CLI
  turn-progress watchdog deadline plus a safety margin. This prevents a
  legitimate long tool call, which may emit `tool-start` and remain silent
  until `tool-finish`, from being killed before the CLI watchdog can decide.
  Configuration below that floor is invalid and disables autonomous
  stall-based recovery for that observation; it resolves non-green/unknown
  and emits a configuration diagnostic rather than silently clamping or
  killing early. If the watchdog deadline cannot be resolved, progress
  staleness alone is not restart authority. After threshold and confirmation,
  use the existing restart path with a distinct reason.
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

`test_wrapped_terminal_stale_runtime_cannot_be_healthy_idle` supplies a valid
resolved runtime observation in `terminal` phase with a success outcome but
stale progress, a fresh heartbeat, and a live wrapper. Current code ignores
the runtime observation and again returns `HEALTHY_IDLE`. The test requires
the default `CLI_CHILD_UNKNOWN` verdict. It demonstrates why adding selected
rows ahead of the legacy branch is insufficient: the wrapped classifier needs
an explicit default and must be total.

## Implementation test matrix

The implementation includes deterministic synthetic-snapshot tests for:

- idle wrapper without a child remains `HEALTHY_IDLE`;
- terminal phase with stale progress is `CLI_CHILD_UNKNOWN`, never
  `HEALTHY_IDLE`;
- every unenumerated `(phase, brain, progress)` tuple hits the default
  `CLI_CHILD_UNKNOWN` row rather than legacy heartbeat classification;
- active Claude launcher/self is `HEALTHY_WORKING`;
- active Codex launcher exit plus live TUI grandchild is discovered correctly;
- active turn plus confirmed absent child becomes `CLI_CHILD_DEAD`;
- snapshot/ancestry/start-token ambiguity becomes `CLI_CHILD_UNKNOWN`, never
  green and never kill;
- torn, partial, malformed, and parse-failed runtime records become one
  `CLI_CHILD_UNKNOWN` observation without exposing partial fields;
- a progress-sequence decrease for the same valid wrapper/turn is unknown and
  cannot trigger death, stall, or restart;
- a progressing child stays working;
- a live child with an unchanged progress sequence becomes stalled only after
  the configured threshold and confirmation poll;
- the configured stall threshold cannot be less than the resolved per-CLI
  watchdog deadline plus margin, and a long silent tool call is not killed
  before that floor;
- work-heartbeat ticks do not count as progress;
- wrapper/turn generation changes reset the stall/death debounce;
- restart backoff and readiness caps still prevent relaunch storms;
- dead-letter is visible as a terminal outcome and is not reported as a
  successful turn; and
- after the validated bus commits and advances an inbound, a subsequent #72
  child-health restart begins at that advanced cursor and does not reprocess
  the committed inbound; and
- the availability view cannot return `AVAILABLE` for child dead, unknown, or
  stalled states even with a fresh heartbeat.

Windows and POSIX tests should use the shared process-snapshot row contract.
Platform-specific snapshot adapters need focused tests, while the verdict
table remains pure and platform-independent.

## Residual risks and boundaries

- Process presence cannot prove model progress; that is why the runtime
  sequence is required.
- Adapter-event progress is not semantic progress. A tool-retry loop can keep
  emitting accepted adapter events, advance `progress_sequence`, and remain
  `HEALTHY_WORKING` without making useful task progress. Detecting semantic
  loops is outside #72; this design only closes heartbeat-only and
  process/progress false-green states.
- Process ancestry can be lost after reparenting. The design fails safe to
  unknown instead of guessing.
- A same-user process can rewrite consistency files. This design prevents
  accidental false-green states; it is not an authentication boundary.
- A terminal child failure can occur between polls. The wrapper's terminal
  observation remains necessary even when the supervisor never sees the dead
  process.
- This change should surface dead-letter outcomes, but it does not redesign
  dead-letter policy or automatically requeue disposed work.
