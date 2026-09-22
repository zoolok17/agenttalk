# Watched Qwen-on-OVH Trial

Audience: the operator and non-Qwen lead running the short Qwen build trial on
Windows.

This is a **same-user cooperative trial**. It is not process isolation or
secret isolation. A process deliberately acting as the same Windows user can
read or modify the gateway state, tokens, key, or spend ledger. The controls
below bound accidental and crash-driven use; they do not defend against a
hostile same-user process.

## 2026-09-22 endpoint change

Moved from the old per-model host to OVH's unified OpenAI-compatible
endpoint and its `Qwen3.8-27B` deployment:

- Model alias: `Qwen3.5-397B-A17B` -> `Qwen3.8-27B`.
- API base: the old per-model host -> `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1`
  (the model is now selected by name in the request body, not by hostname).
- Tariff (OVH catalog, observed 2026-09-22): settlement EUR 0.40/M input,
  EUR 2.70/M output (was 0.60 / 3.60); reservation stays tariff+20% (EUR
  0.48/M input, EUR 3.24/M output). `MAX_CONTEXT_TOKENS` unchanged at
  262144. Maximum one-attempt reservation is now EUR 0.139102 (was
  0.206439).
- Development child-turn caps raised: 8 -> 64 provider calls, 300 -> 1800
  seconds, EUR 0.50 -> EUR 3.00 of settled/reserved exposure per turn.
  Fail-closed semantics are unchanged: the first ceiling a turn reaches
  still ends it, and later calls in that turn are still refused before
  transport.
- Operator-set spend cap raised: trial cutoff EUR 25 -> EUR 95, soft stop
  EUR 20 -> EUR 90 (external account ceiling stays EUR 100 - see "Fixed
  Trial Policy" below for why the cutoff is EUR 95, not a bare EUR 100:
  `SpendLedger.initialize`'s own envelope check
  (`opening_micro_eur + TRIAL_CUTOFF_MICRO_EUR +
  reservation_cost_micro_eur() <= EXTERNAL_CEILING_MICRO_EUR`) needs real
  headroom below the ceiling for any nonzero opening balance to init at
  all - EUR 95 leaves ~EUR 4.86, comfortably covering a small top-up;
  the operator's initial run itself observes EUR 0 usage for the period,
  so it clears this with room to spare either way).
- Field defect fix: Qwen3.8-27B's reasoning content is now folded into
  content text (`merge_reasoning_content_in_choices: true` on the
  deployment); thinking blocks are never forwarded to the CLI. Without
  it, an interleaved reasoning delta mid-stream could land on the
  Anthropic-passthrough adapter's state machine while it wasn't
  expecting a thinking block, and the CLI aborted the turn with "API
  Error: Content block is not a thinking block".

Both `price_policy_hash` and `child_cap_policy_hash` change as a direct
result (they hash the values above). **This invalidates every previously
accepted dashboard canary and every existing install's manifest**, not just
the ledger's child-cap gate:

- `canary-verify` binds to a `settled` attempt whose stored `policy_hash`
  equals the *current* `price_policy_hash()` (`ovh_gateway.py`,
  `SpendLedger.canary_verify` and the dashboard-canary read path) - a
  canary accepted under the old tariff can never satisfy that check again,
  by design.
- `agenttalk gateway reconfigure` is scoped to an **endpoint-only** change:
  `_reconfigure_endpoint_locked` refuses with `gateway install manifest
  price policy mismatch` whenever the existing install manifest's
  `price_policy_hash` (frozen at `init` time) no longer matches the
  running code's `price_policy_hash()` - which is exactly this change,
  confirmed by reading that guard, not assumed. `SpendLedger.initialize`
  is equally strict the other way: it refuses over an existing ledger
  (`installation_state() != "absent"`), so there is no in-place
  price-policy migration for the ledger either.

So, for an install that predates this change, `reconfigure` alone does
**not** apply it - the operator instead reruns the full one-time setup
against fresh state (same as any other tariff update), then re-accepts the
canary:

```powershell
agenttalk gateway stop --timeout 30
# Back up/remove the existing .agenttalk/gateway config+manifest and the
# %LOCALAPPDATA%\agenttalk-ovh ledger/marker before re-init - `init` refuses
# to run over any of them. Preserve the removed ledger for reconciliation
# records; its balance becomes the new --opening-eur evidence.
agenttalk gateway init --litellm-executable C:\path\to\litellm.exe `
  --opening-eur <observed OVH dashboard balance> `
  --opening-evidence "OVH AI Endpoints dashboard, observed 2026-09-22"
agenttalk gateway task-install
agenttalk gateway start
agenttalk gateway status
agenttalk doctor
# Then the Live Acceptance step below, mandatory again under the new hash:
agenttalk gateway canary-verify ATTEMPT_ID --dashboard-delta-eur OBSERVED_DELTA
```

A brand-new install (no prior `.agenttalk/gateway` state) just follows
"One-Time Morning Setup" below unchanged - `init` was never going to hit
the mismatch above since there is nothing to mismatch against yet.

## 2026-09-22 caps removed by operator decision; the 95/100 EUR ledger envelope is the only limit

Operator decision, 16:45Z: the wrapped qwen coding turn died twice on
trial-policy caps, not on the model itself - once on the 1800s wall clock
(46 calls in, still in setup) and once on the 4096-token per-call output
cap (a code-writing response overran it once reasoning was folded into
text). Both times the wrapper re-spawned the *same* session id, whose
transcript file already ended in the API error; the CLI refuses
`--session-id` for a file that already exists, so every retry died on a
broken pipe and the message dead-lettered after three attempts (see items
9-10 below for the actual wrapper fix). The operator's call: remove the
trial-policy caps below the ledger envelope entirely - "let it show what
it can do; the only cap is the 100 euro limit."

- `MAX_OUTPUT_TOKENS`: 4096 -> 32768. Not confirmed as the model's actual
  output ceiling from any local LiteLLM/OVH data - no `Qwen3.8-27B` entry
  exists in this venv's model database for any provider; the closest OVH
  entry is a different model generation. Using the lead's own fallback
  value, stated here rather than silently assumed. `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
  (`OVH_QWEN_CLAUDE_MAX_OUTPUT` in `wrapper/run.py`) is kept pinned equal
  to it, 32768 - it was drifted from the gateway's own cap before this
  change (the code forced 4096, not the previously-documented 2048; both
  now read the same constant).
- `CHILD_TURN_MAX_CALLS`: 64 -> 100000. `CHILD_TURN_MAX_SECONDS`: 1800 ->
  86400 (24h). `CHILD_TURN_MAX_MICRO_EUR`: EUR 3.00 -> EUR 95.00, equal to
  the trial cutoff - so the ledger's own trial-cutoff/external-ceiling
  checks are the only spend limits a turn can actually hit; the per-turn
  call/cost/wall-time caps above are now wide enough that they exist only
  as a fail-closed backstop, never the live-run reason. `CHILD_CAP_SCHEMA_VERSION`
  bumps 2 -> 3 (the schema embeds these values directly), so an existing
  ledger's stored schema version now mismatches and blocks until it is
  re-initialized under the new caps, the same fail-closed pattern as the
  1 -> 2 bump.
- The larger `MAX_OUTPUT_TOKENS` makes `reservation_cost_micro_eur()`
  grow (reserve rates apply across the full `MAX_CONTEXT_TOKENS`/
  `MAX_OUTPUT_TOKENS` worst case): EUR 0.139102 -> EUR 0.231999.
  `TRIAL_CUTOFF_MICRO_EUR` did **not** need to move for this - the
  envelope check (`opening + 95_000_000 + reservation_cost_micro_eur() <=
  100_000_000`) still holds at the new, larger reservation cost, for both
  a zero opening balance and the docs' own EUR 0.58 fixture, with
  comfortable headroom either way (confirmed directly, not assumed - see
  "Fixed Trial Policy" below for the exact margin).

Both hashes change: `price_policy_hash` because `MAX_OUTPUT_TOKENS` and
the derived `worst_case_micro_eur` are part of `price_policy()`'s own
`reservation` sub-object (confirmed directly, not assumed, after nearly
repeating the same "unaffected" mistake corrected in an earlier commit on
this branch - checked the actual dict this time); `child_cap_policy_hash`
via `max_calls`/`max_micro_eur`/`max_seconds`/`reservation_micro_eur` and
the schema-version bump. New values: `price_policy_hash` =
`6df40ecdf2c22a9d06a73c2b2d7090b40d722d590e04237b19c903cb034dd5c2`;
`child_cap_policy_hash` =
`47d62e620e762af37b404d22919c89d7739baf8efce4ca6cfc451ca8b159fc14`.

### item 9: a fresh session id after a failed fresh turn

Root cause of the "every retry died on a broken pipe" symptom above: a
FAILED fresh (`--session-id`, not `--resume`) claude turn still leaves the
CLI's session transcript file on disk - the file is created the moment the
process spawns, whether or not the turn completes. `make_drive()`'s
`drive()` closure only reset the session id on a failed `--resume` turn
(after the existing K=2 session-attributable ledger); a failed FRESH turn
left `state.turns == 0` and the same `claude_session_id`, so the next
`build_turn()` call reused `--session-id` with that same, now-already-
created id - which the CLI refuses ("session already exists"), a spawn/
pipe failure with no useful diagnostic, masking the real error.

Fix (`wrapper/run.py`, `make_drive()`): reuse the existing
`session.reset_claude_session()` (mints a new uuid4, clears
`resume_available`) from the failure fallthrough when `cli == "claude"`,
the turn was NOT a `--resume` attempt, and the failure isn't
`CLASS_CONFIG_BLOCKED` (no spawn happened, no session file exists, nothing
to reset). Unlike the resume path's K=2 ceiling, a fresh-turn failure
mints a new id after just ONE failure - a fresh-turn failure is already
attributable to this specific spawn, not global session pressure.

Scoped to `make_drive()` only, not `make_cadence_drive()`: `ovh-qwen` does
not support `wrap --lead-loop` (see "One-Time Morning Setup" / the
backend-profile guardrail above), so the cadence path is architecturally
unreachable for this backend and was deliberately left untouched.

### item 10: close the ledger child turn on dead-letter dispose

A durable message's child turn (`SpendLedger.open_child_turn`, keyed on
`(agent, message_id)`) is designed to stay `'open'` and accumulate call/
cost exposure across every retry of that SAME message - that's the whole
point of the per-message envelope. But once a message is dead-lettered
(the loop's cursor has advanced past it; it will not be retried through
the normal path again), the row was left `'open'` for up to
`CHILD_TURN_MAX_SECONDS` (86400s/24h) with nothing left to do.

Fix: `SpendLedger.close_child_turn()` (`ovh_gateway.py`, next to
`open_child_turn`) eagerly transitions an OPEN row to `'expired'` with a
caller-supplied reason - state-machine-safe (a no-op unless the row is
currently `'open'`; never invents a state outside the schema's
`CHECK ('open','capped','expired')`). `wrapper/run.py`'s new
`close_ovh_child_turn_on_dead_letter()` calls it for the `ovh-qwen`
backend profile only, wired into `cli.py`'s existing
`on_runtime_dead_letter` hook alongside `runtime_writer.dead_letter()`.
Best-effort: a `GatewayError` (held/misconfigured/uninitialized gateway)
is swallowed, since the dispose has already succeeded and the cursor has
already advanced by the time this runs.

Deliberately scoped to dead-letter ONLY, never an ordinary attempt
failure the wrapper intends to retry: closing on every failure would turn
the very next legitimate retry of the SAME message into a permanent
`ChildTurnCapExceeded`/`config_blocked` dead end, since `child_turns` has
no way to reopen a fresh row for an existing `(agent, message_id)` key
once closed.

## Fixed Trial Policy

- Provider route: Claude Code -> `127.0.0.1:4000` -> LiteLLM on
  `127.0.0.1:4001` -> OVH OpenAI Chat Completions.
- Model: `Qwen3.8-27B` only.
- Settlement rates: OVH's EUR tariff, EUR 0.40/M input tokens and EUR 2.70/M
  output tokens.
- Reservation rates: the tariff plus 20%, EUR 0.48/M input tokens and EUR
  3.24/M output tokens.
- Gateway maximum output: 32768 tokens (not confirmed as the model's
  documented ceiling from local data - see the 2026-09-22 caps section
  above); the wrapped Claude child's `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is
  pinned equal to it, 32768. Maximum input context: 262144 tokens.
  Maximum public request body: 512 KiB.
- Maximum one-attempt reservation: EUR 0.231999.
- Maximum one wrapped child turn: 100000 provider calls, EUR 95.00 of
  settled or reserved exposure (equal to the trial cutoff below - the
  ledger's own cutoff/ceiling are the only limits a turn can actually
  hit), and 86400 seconds (24h) from its first durable opening. The first
  reached ceiling closes that turn; later calls are refused before transport.
- Trial cutoff: EUR 95; operator soft stop: EUR 90.
- External account ceiling: EUR 100. Initialization and readiness require the
  operator-observed opening balance plus the EUR 95 trial cutoff plus one
  maximum reservation to remain within this ceiling - not a bare EUR 100
  cutoff, because that check (`SpendLedger.initialize`'s
  `_assert_external_envelope`) needs headroom for one worst-case
  reservation (currently EUR 0.231999) on top of any nonzero opening
  balance; EUR 95 leaves ~EUR 4.77 of margin, which the trial's own
  per-period cutoff check (below) still bounds monthly spend against once
  a ledger is running. Every admission also checks cumulative committed
  spend across all UTC periods plus unresolved reservations against the
  same EUR 100 ceiling - the true, absolute lifetime hard stop.
- Provider attempts: one. LiteLLM, router, and front retries are disabled.

The policy hash is persisted in the install marker, ledger, config manifest,
runtime marker, readiness result, and status output. A mismatch blocks startup
or transport.

## Boundaries

The public front accepts only `POST /v1/messages` or the Claude Code compatibility
form `POST /v1/messages?beta=true`. Both forms require a valid message-scoped child
capability and the literal `Host: 127.0.0.1:4000`. The
installation's front token authorizes capability issuance by the trusted
wrapper controller; it does not authorize paid requests. The front rejects a
static front token, a missing or different Host,
every Origin header, every other path or method, excess body size, excess token
limits, and concurrent work. The front never forwards `/health`, models,
Chat Completions, admin, UI, docs, config, or key routes.

LiteLLM's wider API remains present on the separate internal loopback port. It
is not forwarded by the front; model and administration routes require the
internal LiteLLM master key, while LiteLLM's liveliness route may remain
unauthenticated. That internal key has full control over this LiteLLM instance.
Both ports are bound to literal IPv4 `127.0.0.1`; neither uses `localhost`,
`0.0.0.0`, or `::`.

`store: false` is forced in the generated single-deployment config. Telemetry
is disabled and there is no spend/success callback. The managed runner combines
LiteLLM stdout and stderr into a redacted rotating log at
`%LOCALAPPDATA%\agenttalk-ovh\gateway\litellm.log`. The current log and two
backups are each capped at 1 MiB. Stable gateway errors and retained diagnostics
do not include raw upstream bodies, Authorization values, the internal URL, or
known key/token values.

## One-Time Morning Setup

Do these steps only with the operator present. Do not put the OVH key in the
repository, `.agenttalk`, `supervisor.json`, argv, logs, status, doctor output,
or AgentTalk messages.

1. Verify `OVH_KEY` and `ANTHROPIC_API_KEY` are absent from the shell that will
   start the supervisor.
2. Put the OVH key at
   `%LOCALAPPDATA%\agenttalk-ovh\api_key.txt`. The gateway runner alone reads
   it and passes it to LiteLLM through the child environment.
3. Initialize the ledger, config, tokens, and install manifest once:

   ```powershell
   agenttalk gateway init --litellm-executable C:\path\to\litellm.exe `
     --opening-eur 0.58 `
     --opening-evidence "OVH AI Endpoints dashboard, observed 2026-07-16 morning"
   ```

   Initialization is explicit and one-time. Service startup never creates or
   resets a missing ledger. A partial, corrupt, deleted, rolled-back, or
   policy-mismatched ledger blocks startup. The first billing period is seeded
   with the operator-provided month-to-date opening balance; status and doctor
   surface its amount, evidence, observation timestamp, and period.

4. Install the project-scoped current-user Scheduled Task, then start it:

   ```powershell
   agenttalk gateway task-install
   agenttalk gateway start
   agenttalk gateway status
   agenttalk doctor
   ```

The task name is derived from canonical project identity. Installation is
idempotent for an exact match and refuses a foreign or mismatched task. The
executable, arguments, and working directory must round-trip exactly; the
current-user principal is compared by resolved Windows SID because Task
Scheduler normalizes account names to SIDs. The Scheduled Task is required for
operational and worker readiness; direct `gateway run` is not a supported
worker launch mode. Startup allows LiteLLM up to 120 seconds to become live on
a cold boot, but fails immediately if the child exits. It then verifies the
manifest, config hash, ledger, runtime process identity, both binds, a no-secret
negative-auth public-front probe, and internal liveliness before reporting
ready.

The non-secret LiteLLM config, task identity, install manifest, and runtime
marker intentionally live in the project's gitignored `.agenttalk/gateway`
directory. The provider key, gateway tokens, spend ledger, and bounded child
log remain under `%LOCALAPPDATA%\agenttalk-ovh`.
Status and doctor use only the local liveliness route; they do not call OVH or
spend money.

## Wrapped Worker Configuration

Add the worker to the roster with non-authority metadata:

```powershell
agenttalk roster add qwen-dev-1 --trust-class external-worker
```

If it already exists:

```powershell
agenttalk roster set-trust-class qwen-dev-1 external-worker
```

Merge these fields into the generated wrapped-Claude entry in
`.agenttalk/supervisor.json`; keep its normal Python wrapper launch and real
Claude executable tail:

```json
{
  "cli": "claude",
  "wrapped": true,
  "model": "Qwen3.8-27B",
  "backend_profile": "ovh-qwen",
  "trust_class": "external-worker"
}
```

Do not add an `env` object to this entry. The profile constructs the child
environment from an empty map, passes only the named safe OS and `AGENTTALK_*`
variables, and injects the loopback URL. Immediately before each real model
spawn, the trusted wrapper binds the immutable inbound message ID to a durable
turn and replaces the controller-only front token with that turn's opaque
capability. Executable preflight and the model child never receive the issuer
token. The profile excludes ambient
`ANTHROPIC_API_KEY`, `OVH_KEY`, and `ANTHROPIC_AUTH_TOKEN`. Every non-Qwen
profile retains its prior environment behavior.

The profile also forces `CLAUDE_CONFIG_DIR` to the selected Store project's
`.agenttalk/gateway/claude-profile` directory. For the shared-bus live proof this
is the main project profile, even when the child executes in a separate workspace.
It never inherits the operator's `HOME`, `USERPROFILE`, or ambient Claude profile
path.

Gateway operational readiness remains available before the live canary so the
operator can run that canary. `wrap --loop` separately requires worker/spend
readiness: the accounting ledger must be ready and its policy-bound dashboard
canary must be accepted. The roster and supervisor trust classes must also
agree, the model and CLI are pinned, and provider keys are absent from the
supervisor environment. A failed readiness check creates the normal durable
`config_blocked` hold before any message is consumed.

## Spend and Failure Semantics

Before every provider transport, SQLite `BEGIN IMMEDIATE` durably records
a unique attempt reservation and consumes the next slot in the same child-turn
transaction. A reservation rejected by the call, cost, or wall-time ceiling
does not create a provider attempt. Replaying or restarting the same immutable
message reuses its original durable turn bucket. SQLite `synchronous=FULL`
commit is the sole
transaction durability authority; there is no fallible second flush after a
committed terminal transition. Admission counts committed spend plus every
unresolved reservation. The concurrency permit remains held through settlement
or durable hold.

`ovh-qwen` does not support `wrap --lead-loop`. Cadence turns have no immutable
inbound message scope, so the wrapper and supervisor bootstrap check reject
that combination instead of launching an uncapped child.

For an existing schema-v1 trial ledger, keep a manual hold in place, reconcile
every unresolved attempt, and run `agenttalk gateway cap-install` from the
cap-aware build before enabling worker spend. The migration binds the current
front token as the issuer, advances both the ledger and install marker to
schema v2, and is retryable if marker projection fails after the database
commit. Schema-v1 gateway code rejects the migrated ledger, which prevents a
rollback to static-token paid admission.

A complete response with exact-model, present, positive integer token usage is
settled to the original UTC admission period. Missing or invalid usage,
provider failure after possible send, timeout, stream cancellation, client
disconnect, callback/settlement error, or process crash retains the full
reservation and blocks restart and later calls. Reservations never clear at a
month boundary. Clock rollback and impossible period jumps block the ledger.

Inspect the attempt and reconcile only from provider/dashboard evidence:

```powershell
agenttalk gateway status
agenttalk gateway reconcile ATTEMPT_ID --outcome no-send --reason "provider confirms no request"
agenttalk gateway reconcile ATTEMPT_ID --outcome charge-reserve --reason "charge remains uncertain"
```

`no-send` requires recorded provider evidence and cannot erase an already
recorded charge. `charge-reserve` commits the full conservative reservation.
The reconciliation surface never accepts a caller-supplied actual cost; valid
terminal usage settles automatically from the completed provider response.

Use a service hold when the live canary or dashboard does not match the pinned
price policy:

```powershell
agenttalk gateway hold --reason "dashboard mismatch"
agenttalk gateway clear-hold --reason "operator reconciled mismatch"
```

Clearing a manual hold is refused while any attempt remains unresolved. Clearing
a dashboard mismatch hold does not admit a worker while the persisted canary is
still absent or mismatched; a fresh accepted canary is required.

## Stop and Recovery

```powershell
agenttalk gateway stop --timeout 30
```

Stop uses `.agenttalk/gateway/gateway.kill`, the same actions-disabled
convention as the supervisor. It rejects new work, drains for a bounded period,
and lets the runner terminate its verified LiteLLM child. If bounded drain
expires, Task Scheduler ends the runner; stop then requires the runtime marker
to be gone and both exact sockets to be bindable, and reports an error instead
of claiming success if an orphan remains. It never kills a process merely
because it owns a port. Task restart is bounded to three attempts at one-minute
intervals; persistent configuration, authentication, policy, and ledger
failures remain stopped and visible after that ceiling.

## Governance

`external-worker` is worker and breadth-review evidence only. It must never be
lead, operator-facing, a Tier-3 reviewer, release actor, close/signoff/shared
path approver, or counted quorum. Roster mutations prevent assigning lead,
operator-facing, or signoff-candidate eligibility to this trust class.
External-worker trust is sticky: removal creates a permanent non-rebindable
tombstone, rename carries the trust class, and `init --force` cannot silently
reclassify or resurrect the identity.

For this watched trial, the non-Qwen lead additionally controls reviewer
selection. Every Qwen-built final SHA requires two distinct non-Qwen,
cross-family reviewers plus the non-Qwen lead. Keep the worker in a disposable
clone with no push, merge, release, GitHub, or OVH credentials and only
reversible work.

## Live Acceptance

The live acceptance step is deliberately not part of automated tests. With the
operator watching the OVH dashboard, run one streamed Claude Code
Read/Edit/Read turn, then enforce the observed nonzero dashboard delta against
the settled attempt:

```powershell
agenttalk gateway canary-verify ATTEMPT_ID --dashboard-delta-eur OBSERVED_DELTA
```

The delta must be nonzero and within 10% of the ledger's tariff-derived
settlement. The deterministic 1000-input/100-output fixture settles to 670
micro-EUR, so its tolerance is 67 micro-EUR. The command persists the numeric
comparison; zero or out-of-tolerance deltas set a durable
`dashboard_canary_mismatch` hold and return nonzero before the worker launches.
