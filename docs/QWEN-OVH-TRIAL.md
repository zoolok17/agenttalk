# Watched Qwen-on-OVH Trial

Audience: the operator and non-Qwen lead running the short Qwen build trial on
Windows.

This is a **same-user cooperative trial**. It is not process isolation or
secret isolation. A process deliberately acting as the same Windows user can
read or modify the gateway state, tokens, key, or spend ledger. The controls
below bound accidental and crash-driven use; they do not defend against a
hostile same-user process.

## Fixed Trial Policy

- Provider route: Claude Code -> `127.0.0.1:4000` -> LiteLLM on
  `127.0.0.1:4001` -> OVH OpenAI Chat Completions.
- Model: `Qwen3.5-397B-A17B` only.
- Settlement rates: OVH's EUR tariff, EUR 0.60/M input tokens and EUR 3.60/M
  output tokens.
- Reservation rates: the tariff plus 20%, EUR 0.72/M input tokens and EUR
  4.32/M output tokens.
- Maximum output: 4096 tokens; maximum input context: 262144 tokens.
- Maximum one-attempt reservation: EUR 0.206439.
- Trial cutoff: EUR 25; operator soft stop: EUR 20.
- External account ceiling: EUR 100. Initialization and readiness require the
  operator-observed opening balance plus the EUR 25 trial cutoff plus one
  maximum reservation to remain within this ceiling. Every admission also
  checks cumulative committed spend across all UTC periods plus unresolved
  reservations against the same EUR 100 ceiling.
- Provider attempts: one. LiteLLM, router, and front retries are disabled.

The policy hash is persisted in the install marker, ledger, config manifest,
runtime marker, readiness result, and status output. A mismatch blocks startup
or transport.

## Boundaries

The public front accepts only an authenticated exact `POST /v1/messages` with
the literal `Host: 127.0.0.1:4000`. It rejects missing or different Host,
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
  "model": "Qwen3.5-397B-A17B",
  "backend_profile": "ovh-qwen",
  "trust_class": "external-worker"
}
```

Do not add an `env` object to this entry. The profile constructs the child
environment from an empty map, passes only the named safe OS and `AGENTTALK_*`
variables, and injects the loopback URL and front token. It excludes ambient
`ANTHROPIC_API_KEY`, `OVH_KEY`, and `ANTHROPIC_AUTH_TOKEN`. Every non-Qwen
profile retains its prior environment behavior.

The profile also forces `CLAUDE_CONFIG_DIR` to the disposable clone's
`.agenttalk/gateway/claude-profile` directory. It never inherits the operator's
`HOME`, `USERPROFILE`, or ambient Claude profile path.

Gateway operational readiness remains available before the live canary so the
operator can run that canary. `wrap --loop` separately requires worker/spend
readiness: the accounting ledger must be ready and its policy-bound dashboard
canary must be accepted. The roster and supervisor trust classes must also
agree, the model and CLI are pinned, and provider keys are absent from the
supervisor environment. A failed readiness check creates the normal durable
`config_blocked` hold before any message is consumed.

## Spend and Failure Semantics

Before the single provider transport, SQLite `BEGIN IMMEDIATE` durably records
a unique attempt reservation. SQLite `synchronous=FULL` commit is the sole
transaction durability authority; there is no fallible second flush after a
committed terminal transition. Admission counts committed spend plus every
unresolved reservation. The concurrency permit remains held through settlement
or durable hold.

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
settlement. The deterministic 1000-input/100-output fixture settles to 960
micro-EUR, so its tolerance is 96 micro-EUR. The command persists the numeric
comparison; zero or out-of-tolerance deltas set a durable
`dashboard_canary_mismatch` hold and return nonzero before the worker launches.
