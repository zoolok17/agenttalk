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
- Settlement rates: EUR 0.71/M input tokens and EUR 4.25/M output tokens.
- Reservation rates: EUR 0.852/M input tokens and EUR 5.10/M output tokens.
- Maximum output: 4096 tokens; maximum input context: 262144 tokens.
- Maximum one-attempt reservation: EUR 0.244237.
- Trial cutoff: EUR 25; operator soft stop: EUR 20.
- External account ceiling: EUR 100. Initialization and readiness require the
  operator-observed opening balance plus the EUR 25 trial cutoff plus one
  maximum reservation to remain within this ceiling.
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
is disabled, there is no spend/success callback, and the managed runner sends
LiteLLM stdout and stderr to `DEVNULL` rather than retaining its access or error
output. Stable gateway errors do not include raw upstream bodies,
Authorization values, the internal URL, or tokens.

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
idempotent for an exact match and refuses a foreign or mismatched task. Startup
verifies the task, manifest, config hash, ledger, runtime process identity,
both binds, a no-secret negative-auth public-front probe, and internal
liveliness before reporting ready.
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

`wrap --loop` refuses to launch this worker unless the gateway is fully ready,
the roster and supervisor trust classes agree, the model and CLI are pinned,
and provider keys are absent from the supervisor environment. A failed
readiness check creates the normal durable `config_blocked` hold before any
message is consumed.

## Spend and Failure Semantics

Before the single provider transport, SQLite `BEGIN IMMEDIATE` durably records
a unique attempt reservation and flushes it. Admission counts committed spend
plus every unresolved reservation. The concurrency permit remains held through
settlement or durable hold.

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

Clearing a manual hold is refused while any attempt remains unresolved.

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

For this watched trial, the non-Qwen lead additionally controls reviewer
selection. Every Qwen-built final SHA requires two distinct non-Qwen,
cross-family reviewers plus the non-Qwen lead. Keep the worker in a disposable
clone with no push, merge, release, GitHub, or OVH credentials and only
reversible work.

## Live Acceptance

The live acceptance step is deliberately not part of automated tests. With the
operator watching the OVH dashboard, run one streamed Claude Code
Read/Edit/Read turn, verify the exact model and ledger settlement against OVH,
and place a durable hold on any mismatch before launching the wrapped worker.
