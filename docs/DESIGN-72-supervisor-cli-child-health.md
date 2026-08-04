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
  "schema_version": 2,
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
  "cli_launcher_lifetime": {
    "source": "windows_get_process_times_v1",
    "creation_filetime": "134141725800000000",
    "exit_filetime": "134141725850000000"
  },
  "progress_sequence": 9,
  "last_progress_at": "2026-07-24T15:03:08.000000Z",
  "last_outcome": "success|failed|dead_letter|null",
  "updated_at": "2026-07-24T15:03:08.000000Z"
}
```

`cli_launcher_lifetime` is normally `null` while the launcher is live and on
platforms without the retained-handle certificate. On Windows, after the
retained `Popen` handle signals, the wrapper records exact creation/exit
FILETIMEs from `GetProcessTimes`; a descendant behind an already-exited
launcher is admissible only when its exact `start_filetime` lies strictly
between those bounds. The lifetime object is all-or-nothing: its source and both
positive decimal FILETIMEs are required, and creation must precede exit.
Authoritative `complete`/`absent` Windows owned-tree entries require a positive
decimal `start_filetime`. `invalid`/`truncated` HOLD entries may retain null so
their failure evidence stays readable, but null grants no identity authority.
Linux boot-ID/start-ticks tokens are exact without FILETIME. If prior proof
recorded an exact FILETIME, a current row with that field missing is ambiguous.
Schema-v1 files remain read-compatible and are normalized to schema v2 with a
null lifetime, but all new writes are v2. Booleans are not accepted as
schema-version integers.

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
legacy fresh-heartbeat classifier. This table applies only after the owned-tree
gate passes: invalid/truncated tree evidence returns
`PROCESS_TREE_INVALID`/`PROCESS_TREE_TRUNCATED` before runtime parsing or
restart-marker consumption. The runtime table is deliberately total:

| Runtime phase | CLI brain | Progress | Verdict | Automatic action |
| --- | --- | --- | --- | --- |
| valid idle, live wrapper, fresh heartbeat | not required | n/a | `HEALTHY_IDLE` | none |
| valid idle, wrapper or heartbeat not green | not required | n/a | existing non-green wrapper verdict | existing wrapper path |
| starting, inside spawn grace | not yet known | n/a | `CLI_CHILD_STARTING` | none |
| active | alive | recent | `HEALTHY_WORKING` | none |
| active | dead, first observation | n/a | `CLI_CHILD_MISSING` | none |
| active | dead, confirmed | n/a | `CLI_CHILD_DEAD` | existing restart path |
| active | unknown | any | `CLI_CHILD_UNKNOWN` | warn, no kill |
| active | alive | stale, first observation | `CLI_CHILD_STALL_SUSPECT` | none |
| active | alive | stale, fresh heartbeat, no live watchdog | `CLI_CHILD_STALLED` | none |
| active | alive | stale past coalescing allowance, stale authoritative heartbeat or live-watchdog floor | `CLI_CHILD_STALLED` | existing stuck path |
| terminal success/finalizing, fresh heartbeat | not required | recent | `HEALTHY_WORKING` | none |
| terminal failure/dead-letter, fresh heartbeat | not required | n/a | `TURN_FAILED` | wrapper policy/notify |
| terminal, fresh heartbeat | any | stale or unclassified | `CLI_CHILD_UNKNOWN` | warn, no kill |
| terminal, stale heartbeat | any | any | existing non-green wrapper verdict | existing wrapper path |
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

1. evaluate the strict bounded owned tree. Invalid or truncated evidence
   returns `WARN_ONLY` with `PROCESS_TREE_INVALID` or
   `PROCESS_TREE_TRUNCATED`; do not consume a restart marker and do not enter
   child-liveness policy;
2. strictly parse and bind the complete runtime record; on any failure, return
   `CLI_CHILD_UNKNOWN`;
3. for `idle`, run the existing wrapper/heartbeat sub-classifier and return its
   result;
4. for `starting`, `active`, or `terminal`, return a matching row above; and
5. otherwise return `CLI_CHILD_UNKNOWN`.

There is no continuation from steps 4 or 5 into the legacy fresh-heartbeat
branch.

### 5. Death, wedge, and restart policy

Only an executable supervisor poll may advance either two-poll confirmation
counter. Read-only operator views use the same decision table but may only
consume confirmation already persisted by an executable poll; reading the same
process snapshot again is not a second poll. This observation/transition
boundary keeps status, diagnostics, and coordination projections from
manufacturing the evidence required for `CLI_CHILD_DEAD` or
`CLI_CHILD_STALLED`.

The boundary is executable, not conventional. Plain `supervise --plan` and the
generated host's `-DryRun` route call `observe_actions`. The live generated host
alone calls the hidden `--executable-poll` route with its current supervisor
instance token and process identity. `observe_actions` positively projects source
facts and cannot return kill targets, state/marker transitions, launch arguments,
or archive authority; adding a new executor field therefore cannot expose it to
an operator surface by default.

Child death and child stall are different failures:

- **Dead:** the active-turn brain is absent from a trustworthy snapshot.
  Confirm across two polls for the same wrapper/turn generation, outside a
  short spawn/handoff grace. Then use the existing `STUCK_RECOVER` executor
  path, start-guarded kill targets, exponential backoff, and readiness cap.
- **Wedged:** the brain is alive but `progress_sequence` has not advanced past
  the per-CLI turn-progress threshold. Crossing that threshold immediately
  produces a non-green stall observation, but progress staleness alone is not
  kill authority while heartbeat remains fresh. Recovery additionally requires
  either an authoritative stale heartbeat or an effectively-live per-turn
  watchdog whose deadline plus safety margin has actually elapsed. This
  prevents a legitimate long tool call, which may emit `tool-start` and remain
  silent until `tool-finish`, from being killed while its bounded
  work-heartbeat still proves liveness. A configured threshold below the live
  watchdog floor never disables watchdog authority: that branch waits for its
  own floor regardless of opt-in. `allow_low_stuck_after=true` instead makes
  the low value authoritative for the earlier stale-heartbeat branch. A stale
  authoritative heartbeat remains independent and is never gated by
  watchdog-floor validity. Finally,
  recovery requires the observed durable progress age to include the maximum
  writer-coalescing interval, so hidden in-memory progress cannot consume the
  safety margin.
- **Unknown:** the evidence cannot establish death or health. It is not green,
  but it must not authorize a kill or restart. Emit a rate-limited warning and
  retry observation.

#### Recovery authority audit

For the active-live-child rows, let:

- `P` be the supervisor's same-turn durable progress age;
- `S` be resolved `stuck_after_seconds`;
- `C` be `MAX_PROGRESS_WRITE_INTERVAL_SECONDS`;
- `F` be the valid live-watchdog deadline plus safety margin;
- `H` be `heartbeat_stale && can_confirm_stuck`;
- `W` be `watchdog_effectively_live`; and
- `O` be `allow_low_stuck_after is true` using a literal JSON boolean; strings,
  numbers, null, and omission are false. It participates in the low-threshold
  safety part of `H`, not in watchdog branch B.

After the same-generation two-poll debounce, the complete stall recovery
inequality is:

`P >= S + C && (H || (W && F is valid && P >= max(S, F) + C))`.

The two terms inside the parentheses are independent. In particular, an
invalid or unmet `F` makes only the watchdog term false; it cannot veto `H`.
The post-authority policy barriers below still apply before an action is
emitted.

**Exhaustiveness method.** An AST inventory of `_plan_one` yields 40 concrete
`_result` / `_healthy` exits after excluding the `_healthy` helper's own return.
Each exit from the manual-marker section through the final healthy fallback is
mapped to one row below; equivalent exits that differ only in warning rate
limiting share a row. The separate policy-barrier table maps every exit after
recovery authority has been established. The inventory is rechecked with
`rg "return _result|return _healthy"` whenever this planner changes.

| Row | Condition | Authority branch | Inputs consulted | Verdict and action | Regression |
| --- | --- | --- | --- | --- | --- |
| A0 | `auto_restart` false or report missing | planner exclusion | configuration/report presence | no plan, no action | `test_plan_ignores_unsupervised_or_unreported_agents` |
| A0T | wrapped owned tree is invalid or truncated | process-authority HOLD before restart/runtime policy | strict owned-tree status, counts, reason, Attention projection | `PROCESS_TREE_INVALID` or `PROCESS_TREE_TRUNCATED`; `WARN_ONLY`, no kill, no marker consumption | `test_owned_process_tree_bound_holds_and_escalates_when_truncated`, `test_owned_process_tree_unreadable_live_wrapper_nonce_is_hold` |
| A1 | restart marker lacks current authority | manual denial | marker authority and revalidation | `RESTART_UNAUTHORIZED`; none | `test_unauthorized_restart_marker_refuses_and_stays_visible` |
| A2 | protected marker lacks force/live-kill acknowledgement, or marker is cooling down | manual denial | protected status, explicit acknowledgements, cooldown | `REFUSE_PROTECTED`, `LIVE_PROTECTED_REFUSED`, or `RESTART_COOLDOWN`; none | `test_protected_marker_without_force_is_refused_and_stays_visible`, `test_fresh_protected_force_requires_second_live_kill_ack`, `test_restart_cooldown_defers_without_consuming_marker` |
| A3 | marker is fully authorized | manual override | marker authority, protected acknowledgements, cooldown | `MANUAL_RESTART`; `RELAUNCH` bypasses autonomous backoff | `test_scenario_iii_manual_marker_relaunches_and_waits_for_readiness` |
| A4 | consumed marker reaches validated readiness | manual completion | marker id, fresh heartbeat, runtime idle/readiness | `HEALTHY_IDLE`; clear marker | `test_consumed_marker_now_alive_clears` |
| A5 | runtime missing, invalid, torn, unbound, or same-turn sequence regression | none | strict record, wrapper generation, turn generation, sequence high-water | `CLI_CHILD_UNKNOWN`; none | `test_wrapped_missing_runtime_is_unknown_with_rollout_remediation`, `test_wrapped_same_turn_sequence_regression_is_sticky_unknown` |
| A6 | wrapper positively absent with fresh heartbeat / stale heartbeat | none / positive wrapper death | guarded wrapper PID/start, snapshot availability, heartbeat | `WRAPPER_MISSING`; none / recovery candidate | `test_wrapped_idle_absent_wrapper_is_non_green_before_heartbeat_stales`, `test_wrapped_dead_wrapper_recovers_from_every_non_idle_runtime_phase` |
| A7 | valid idle, live wrapper, fresh / stale authoritative heartbeat | none / heartbeat branch A | phase, wrapper identity, heartbeat, `can_confirm_stuck` | `HEALTHY_IDLE`; none / recovery candidate | `test_wrapped_idle_without_cli_child_is_healthy_idle`, `test_wrapped_non_active_runtime_recovery_matrix[idle-stale-heartbeat]` |
| A8 | starting inside / beyond grace | none | phase, updated age, launch generation/grace | `CLI_CHILD_STARTING` / `CLI_CHILD_UNKNOWN`; none | `test_wrapped_non_active_runtime_recovery_matrix[starting-in-grace]`, `[starting-after-grace]` |
| A9 | fresh-heartbeat terminal failure or dead-letter | none | outcome, heartbeat | `TURN_FAILED`; none | `test_wrapped_non_active_runtime_recovery_matrix[terminal-failure]` |
| A10 | fresh-heartbeat terminal success with recent classified progress | none | outcome, `P < S`, heartbeat | `HEALTHY_WORKING`; none | `test_wrapped_non_active_runtime_recovery_matrix[terminal-success-finalizing]` |
| A11 | fresh-heartbeat terminal success with stale or unclassified progress | none | outcome, progress age validity, heartbeat | `CLI_CHILD_UNKNOWN`; none | `test_wrapped_non_active_runtime_recovery_matrix[terminal-success-stale-progress]`, `[terminal-success-unclassified-progress]` |
| A12 | any terminal outcome with stale heartbeat | heartbeat branch A | heartbeat, ordinary stale-recovery guards | recovery candidate | `test_wrapped_stale_live_terminal_uses_existing_recovery_path` |
| A13 | active child ancestry/start evidence unknown, invalid progress sequence, or unmatched tuple | none | current-turn launcher binding, process snapshot, progress schema | `CLI_CHILD_UNKNOWN`; none | `test_wrapped_ambiguous_brain_is_unknown_and_never_killed`, `test_wrapped_active_live_brain_with_invalid_progress_sequence_is_unknown` |
| A14 | active child absent in grace / first poll / second same-generation poll | positive child death | grace, wrapper/turn generation, dead-poll debounce | `CLI_CHILD_STARTING` / `CLI_CHILD_MISSING` / recovery candidate as `CLI_CHILD_DEAD` | `test_wrapped_prior_turn_sequence_does_not_end_current_spawn_grace`, `test_wrapped_active_absent_brain_requires_two_same_generation_polls` |
| A15 | active live child has no real progress inside grace / after grace while `P < S` | none | real-progress timestamp, grace, progress age | `CLI_CHILD_STARTING` / `CLI_CHILD_NO_PROGRESS`; none | `test_wrapped_prior_turn_sequence_does_not_end_current_spawn_grace`, `test_wrapped_active_live_brain_without_progress_below_threshold_is_non_green` |
| A16 | active live child has real progress and `P < S` | none | progress sequence and same-turn observation age | `HEALTHY_WORKING`; none | `test_wrapped_active_stall_recovery_authority_matrix[recent-progress]` |
| A17 | `P >= S`, first confirming poll, or `P < S + C` on the second poll | none | generation, sequence, debounce, coalescing bound | `CLI_CHILD_STALL_SUSPECT` / `CLI_CHILD_STALLED`; none | `test_wrapped_active_stall_recovery_authority_matrix[first-stale-poll]`, `[coalescing-allowance]` |
| A18 | `P >= S + C` and `H` | heartbeat branch A | heartbeat, `can_confirm_stuck`, debounce; no watchdog floor | recovery candidate as `CLI_CHILD_STALLED` | `test_wrapped_active_stall_recovery_authority_matrix[heartbeat-authority-watchdog-off]`, `[heartbeat-authority-low-opt-in]`, `[heartbeat-authority-invalid-watchdog-floor]`, `[heartbeat-authority-zero-watchdog-poll-fallback]` |
| A19 | heartbeat stale but branch A is not authoritative | denied branch A | literal-boolean low-Codex opt-in or Claude work-heartbeat validation | `CLI_CHILD_STALLED`; none | `test_wrapped_active_stall_recovery_authority_matrix[heartbeat-denied-low-no-opt-in]`, `[heartbeat-denied-invalid-work-heartbeat]`, `test_wrapped_low_stuck_opt_in_requires_literal_boolean_true` |
| A20 | heartbeat fresh and watchdog not live | no authority | heartbeat, watchdog-live predicate | `CLI_CHILD_STALLED`; none | `test_wrapped_active_stall_recovery_authority_matrix[fresh-heartbeat-watchdog-off]` |
| A21 | watchdog live, valid `F`, and `P >= max(S, F) + C` | watchdog branch B | watchdog live/floor, progress age, coalescing; `O` cannot veto | recovery candidate as `CLI_CHILD_STALLED` | `test_wrapped_active_stall_recovery_authority_matrix[watchdog-authority-valid-floor]`, `[watchdog-low-opt-in-after-floor]`, `[watchdog-low-without-opt-in-after-floor]` |
| A22 | watchdog floor not yet reached, invalid, or unresolved | denied branch B only | strict live/floor validity; `O` affects wording only | `CLI_CHILD_STALLED`; none unless independent `H` is true | `test_wrapped_active_stall_recovery_authority_matrix[watchdog-low-opt-in-before-floor]`, `[watchdog-low-without-opt-in-before-floor]`, `[watchdog-invalid-floor]`, `[heartbeat-authority-invalid-watchdog-floor]` |
| A23 | wrapper/turn generation or sequence changes between polls | none yet | generation/sequence debounce | reset confirmation; none | `test_wrapped_generation_change_resets_dead_confirmation`, `test_wrapped_generation_change_resets_stall_confirmation` |
| A24 | launch is still inside grace and no child recovery is confirmed | none | launch generation/time, child recovery reason | `LAUNCHING`; none | `test_wrapped_in_grace_does_not_request_brain_discovery` |
| A25 | non-wrapped agent reaches the fresh-heartbeat fallback | none | heartbeat and launch state | `HEALTHY_IDLE`; none | `test_scenario_i_fresh_heartbeat_ignores_missing_brain_snapshot` |

Every recovery candidate then passes through this exhaustive policy barrier
table. These barriers constrain action emission; they do not change the
underlying child verdict.

| Row | Barrier condition | Inputs consulted | Verdict and action | Regression |
| --- | --- | --- | --- | --- |
| G1 | durable config-blocked hold | hold marker | `CONFIG_BLOCKED`; none | `test_wrapped_config_blocked_hold_marker_holds_when_stale_until_restart` |
| G2 | deliberate lead-loop stand-down | exit marker | `LEAD_LOOP_STOOD_DOWN`; none | `test_lead_loop_stood_down_marker_suppresses_relaunch` |
| G3 | lead-loop blocked and incumbent lease still armed | exit marker plus live lease | `LEAD_LOOP_BLOCKED`; none | `test_lead_loop_blocked_marker_holds_only_while_owner_live` |
| G4 | protected agent without manual override | protected role | same failure state; `WARN_ONLY` | `test_stuck_protected_is_warn_only` |
| G5 | stale-heartbeat path but `can_confirm_stuck` false | activity/work-heartbeat/low-threshold guards | `ACTIVE_OR_BUSY`; warn or none | `test_wrapped_codex_stuck_after_below_watchdog_deadline_refuses_restart` |
| G6 | readiness retry cap reached | launch/readiness generation and count | `READINESS_GAVE_UP`; no relaunch | `test_readiness_cap_relaunches_below_cap_then_gives_up` |
| G7 | backoff deadline not reached | `backoff_next_epoch` | failure verdict; `BACKOFF_WAIT` | `test_wrapped_child_dead_respects_backoff_and_readiness_cap` |
| G8 | no barrier, backoff due | readiness cap, backoff, start-guarded targets | failure verdict; `STUCK_RECOVER` | `test_wrapped_stalled_forked_brain_is_an_attributed_kill_target` |

Omitted watchdog settings resolve to bounded defaults. A zero or malformed
explicit watchdog interval/deadline cannot grant branch-B kill authority; it
also cannot veto a valid branch-A heartbeat decision. A zero or malformed
Claude work-heartbeat setting is startup/config-invalid and therefore removes
heartbeat authority rather than creating a false kill. Unknown evidence never
kills. Conversely, positive wrapper/child death and the two independent active
stall branches ensure a genuinely wedged, validly configured agent retains a
recovery path.

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

#### Wrapper turn-exit and idle audit

`on_runtime_idle` means the loop has no unsettled ownership of the just-finished
turn. The entry callback establishes the new wrapper generation before it owns
a record. Once a record is observed, a pending retry, semantic terminal, or
park must retain its non-idle record and must not emit idle. A consumed,
reconciled, disposed, or successfully completed synthetic turn emits idle
exactly at that boundary. Terminal writes remain forced and durable; the
following idle write does not erase `last_outcome`.

**Exhaustiveness method.** The audit inventories both orchestration functions
with an AST/call-site pass: seven continuous and six one-shot `finalize` calls,
two continuous and two one-shot `fail_delivery_or_block` calls, all ten
`_settle_retry_exhaustion` calls, every direct `recv_api.commit`, every
`_commit`, and every `_dispose`. It then traces every `return` / `continue`
reachable from those calls plus the entry, empty-poll, cadence, exception, and
bounded-exit paths. Every commit-gate path below now uses the same
`recv_api.consume_boundary_complete` predicate as the gate's own projection
reconciliation: global cursor coverage for global records and
`max(global cursor, thread_seen)` coverage for scoped records.

| Loop path | Cursor / work state | Runtime transition | `on_runtime_idle` | Regression |
| --- | --- | --- | --- | --- |
| wrapper entry / later empty poll | no record owned | entry writes `idle`; later empty polls preserve it | entry only | `test_continuous_loop_runtime_boundary_matrix[normal_success]`, `test_one_shot_times_out_nonzero_when_request_never_arrives` |
| normal global inbound success without a commit gate | global cursor commits | `starting -> ... -> terminal(success) -> idle` | yes, after commit | `test_continuous_loop_runtime_boundary_matrix[normal_success]` |
| retryable drive failure below a disposition cap | head remains pending | `starting -> ... -> terminal(failed)` | no | `test_continuous_loop_runtime_boundary_matrix[drive_failure]` |
| config-blocked park | head remains parked and non-idle | preflight leaves prior idle, or a begun turn ends `terminal(failed)` | no after a begun failed turn | `test_continuous_loop_runtime_boundary_matrix[config_blocked_park]`, `test_config_blocked_head_parks_with_visible_health_not_frozen_idle` |
| transient gateway hold | head remains pending and is re-driven | begun attempt ends `terminal(failed)` | no | `test_continuous_loop_runtime_boundary_matrix[gateway_hold]` |
| dead-letter / cap disposal | cursor advances to recoverable dead-letter sink | forced synthetic or real `terminal(dead_letter) -> idle` | yes, after the forced terminal write | `test_reconciled_attempt_cap_dead_letters_without_launching_a_child` |
| valid release/end or scoped rescind/supersession control | control commits; no model turn | `idle` | yes, at commit/exit | `test_authorized_release_returns_runtime_to_idle`, `test_rescinded_terminal_control_returns_runtime_to_idle` |
| validated-bus landed-work reconciliation (#73) | already-landed work finalizes and cursor/thread-seen advances; model is not re-driven | prior terminal -> `idle` | yes, on terminal finalization | `test_landed_work_reconciliation_returns_runtime_to_idle`, `test_child_health_restart_does_not_redrive_bus_committed_inbound` |
| semantic terminal with global projection still pending | gate returns terminal but global cursor does not cover the record | prior terminal remains terminal; retry backs off | no | `test_terminal_cas_exhaustion_with_pending_cursor_stays_terminal`, `test_delivery_exhaustion_with_pending_cursor_stays_terminal` |
| global delivery/disposition settlement with completed projection | gate returns terminal and global cursor covers the record | prior terminal -> `idle` | yes | `test_delivery_exhaustion_settlement_returns_runtime_to_idle` |
| global CAS-exhaustion settlement with completed projection | terminal replay completes global cursor projection | prior terminal -> `idle` | yes | `test_terminal_cas_exhaustion_settlement_returns_runtime_and_supervisor_to_idle` |
| successful cadence drive | synthetic turn ends; no cursor involved and loop is again idle | `terminal(success) -> idle` | yes | `test_cadence_runtime_boundary_matrix[cadence_success]` |
| failed cadence drive | controller failure remains actionable; heartbeat is deliberately withheld | `terminal(failed)` | no | `test_cadence_runtime_boundary_matrix[cadence_failure]` |
| direct one-shot success / failed or bounded one-shot | success marks scoped `thread_seen`; failure/bound leaves it pending | success `terminal -> idle`; failure remains terminal | yes only after scoped commit | `test_one_shot_runtime_boundary_matrix[one_shot_success]`, `[one_shot_failure]` |
| one-shot commit-gate finalization with completed projection | `thread_seen` or global cursor covers the scoped record | terminal -> `idle`, then process exits | yes | `test_one_shot_committed_terminal_finalization_returns_runtime_to_idle` |
| one-shot semantic terminal with scoped projection pending | neither global cursor nor `thread_seen` covers the record | terminal remains terminal; bounded process exits nonzero or retries | no | `test_one_shot_terminal_cas_exhaustion_with_pending_thread_seen_stays_terminal` |
| one-shot delivery terminal, committed / pending | scoped projection covers / does not cover the record | `idle` / terminal retained | yes only when committed | `test_one_shot_delivery_terminal_runtime_requires_committed_thread_seen[committed]`, `[pending]` |
| unhandled wrapper exception after turn ownership begins | consume boundary remains pending | last `starting` / `active` / `terminal` record remains | no | `test_unhandled_drive_exception_does_not_publish_idle_after_turn_start` |

Semantic `Resolution.terminal` and `allows_legacy_commit` values select retry
logic, but neither can publish runtime idle. A gate path requires the projection
predicate; a direct `recv_api.commit` must return normally, and dead-letter is
also projection-verified. Synthetic cadence success is the sole no-record
exception.

### 6. Rollout and compatibility

The supervisor and wrapper-runtime writer must ship together. After script
refresh, an older still-running wrapper has no runtime record; it therefore
reports `CLI_CHILD_UNKNOWN`, with no kill or relaunch, and gives an exact
restart/refresh remediation. It must not silently fall back to heartbeat-only
green. The first `idle` observation from the refreshed wrapper establishes the
new baseline.

The bounded owned-tree channel adds an attended migration boundary. Legacy
`managed_pids`, launcher, and brain identities are retained only as bounded
diagnostic evidence and cannot authorize teardown. Stop the supervisor, verify
the complete old wrapper tree by PID/start, re-read the launch nonce from the
live wrapper command line before stopping it, and keep `supervisor.kill`
present. If the live wrapper is unavailable for that re-read, this reset is not
authorized and manual repair is required. With the strict instance marker
absent, use the current nondismissible Attention item's source hash and that
verified nonce:

```powershell
agenttalk supervise --reset-process-tree-ownership --from <liaison> `
  --for <agent> --hold-source-hash <64hex> `
  --verified-launch-nonce <verified-launch-nonce> `
  --acknowledge-no-live-supervisor `
  --acknowledge-owned-processes-stopped `
  --reason "attended owned-tree migration"
```

Under lifecycle-then-config locking, this command rechecks operator-facing
liaison/sole-lead authority, canonical state, kill-switch level, absent strict
instance marker, current Attention hash, matching nonce, strict runtime
agreement on wrapper PID/start/generation, and that each recorded PID/start is
dead or confidently recycled. It never kills or launches. It revokes stale
evidence, records a bounded audit row, and atomically retires the exact old
runtime digest plus its PID/start/generation/nonce boundary. Only that unchanged
sidecar is ignored; changed or new-generation evidence still takes the normal
fail-closed adoption path. The next generation must earn a fresh tree. Missing
nonce/reset evidence refuses and requires manual repair.
Keep the supervisor host stopped, remove `supervisor.kill`, refresh/validate
generated artifacts (`--refresh-scripts` refuses under the kill switch), queue
the restart, then resume the supervisor. Unprovable legacy evidence remains a
nondismissible process-tree HOLD until the attended reset commits; automatic
teardown authority returns only after the new generation earns a complete tree.

A previously complete strict tree can bridge an exited intermediate only in
the same wrapper generation and launch nonce, using the exact prior child
identity and parent edge; new or reparented descendants invalidate it. When all
recorded identities are definitively gone, it becomes an `absent` no-kill
certificate. Unreadable identity or a late child edge rooted at a recorded PID
blocks relaunch.

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
  the configured threshold and confirmation poll, but fresh heartbeat plus no
  live watchdog remains non-killing;
- recovery based on a durable progress age reserves the writer's maximum
  coalescing interval, so a hidden event at `t=4.9s` cannot be killed before
  its true age reaches the configured threshold;
- when a watchdog is live, its recovery branch cannot fire before the resolved
  deadline plus margin; a low threshold waits for that floor regardless of
  opt-in, while opt-in can independently authorize the earlier stale-heartbeat
  branch;
- work-heartbeat ticks do not count as progress;
- wrapper/turn generation changes reset the stall/death debounce;
- restart backoff and readiness caps still prevent relaunch storms;
- dead-letter forces a durable terminal outcome before the consumed loop
  boundary returns to idle; it is never reported as a successful turn; and
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
