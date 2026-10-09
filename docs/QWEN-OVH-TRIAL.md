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
  **Superseded:** merged reasoning is ordinary assistant text to the CLI,
  which re-sends it as input on every later call. Reasoning is now emitted
  as typed thinking blocks and stripped by the front before the CLI sees
  them; see "Reasoning handling" below and `docs/STEP-QWEN-REASONING.md`.

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
**not** apply it - the operator instead re-initialises the install against
fresh state (same as any other tariff update) and re-accepts the canary. The
single runbook for that is the "Upgrade path" section of
`docs/STEP-ENVELOPE-SERVICE-READERS.md` (stop with the runtime that registered
the task, back up and move the ledger and old gateway state aside, unregister
the task if the interpreter changed, `init`, `cap-install`, `task-install`,
`start`, then the Live Acceptance canary below); it is not repeated here.
Preserve the removed ledger for reconciliation records; its balance becomes the
new `--opening-eur` evidence.

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

## Reasoning handling

The front removes the model's reasoning from the response before the Claude CLI
receives it, so reasoning is paid for once as output and is not re-sent as input on
every later call of the tool loop. Usage is still read from the provider's real
reasoning-inclusive counts, so the ledger is unaffected. While reasoning is being
dropped the front sends Anthropic `ping` events so the CLI never sees dead air. The
front records how much was removed per attempt in
`.agenttalk/gateway/reasoning-stripped.jsonl` (attempt id, blocks, characters; no
content).

The route can also carry a fixed reasoning request parameter, rendered under the
deployment's `extra_body` (the only place LiteLLM passes it through; a top-level
`litellm_params` key is silently dropped):

```powershell
agenttalk gateway reconfigure --reasoning-param reasoning_effort=low
agenttalk gateway reconfigure --no-reasoning-param
```

`gateway init` accepts the same `--reasoning-param NAME=VALUE` (repeatable), and
`gateway status` shows the effective `reasoning_params`. Names are lowercase letters,
digits and underscores, optionally prefixed `chat_template_kwargs.`; values are `true`,
`false`, a small integer or a short lowercase word. The parameter is not part of the
price policy, so changing it needs `stop`, `reconfigure`, `start` but no re-init and no
new canary. Which parameter OVH accepts for this model, how to find it with one bounded
probe, and how to measure the effect on cost per card are in
`docs/STEP-QWEN-REASONING.md`.

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

## Envelope at Init

The trial cutoff, soft-stop, and external ceiling above are `agenttalk gateway
init` parameters, not fixed module constants: `--cutoff-eur`, `--soft-stop-eur`,
and `--ceiling-eur`, each in EUR (up to six decimal places, same parsing as
`--opening-eur`). Their defaults are exactly the EUR 95/90/100 figures documented
above, so an unchanged `init` invocation produces the identical `price_policy_hash`
it always has. This lets two independent gateway installs on two different hosts
share one operator-level spend allocation without either one's code changing -
for example, an Ubuntu VM gateway capped at `--cutoff-eur 40` alongside a desktop
gateway left at the default (effectively EUR 95), together never exceeding the
EUR 100 external ceiling each install still enforces on its own. There is no
cross-ledger accounting: each installation's ledger is independent and enforces
only its own envelope; keeping two installs' cutoffs from summing past a shared
comfort level is the operator's own arithmetic when choosing `--cutoff-eur` for
each one, the same way the desktop figures above were chosen.

`init` validates `soft-stop < cutoff <= ceiling` and refuses (`PolicyBlocked`) a
malformed envelope before any state is written. The chosen envelope is pinned
into the ledger's metadata under the same hash discipline as everything else in
"Fixed Trial Policy": a different envelope is a different `price_policy_hash`,
so an existing install's `reconfigure`/`runtime-rebind`/`start` all continue to
refuse a mismatch rather than silently reinterpreting an old ledger under new
numbers. Changing the envelope of an existing install means the same re-init
procedure as any other price-policy change (see "2026-09-22 endpoint change"
above): follow the "Upgrade path" section of
`docs/STEP-ENVELOPE-SERVICE-READERS.md`, running `init` with the new
`--cutoff-eur`/`--soft-stop-eur`/`--ceiling-eur`, and re-accept the canary.

The per-turn cost cap (`CHILD_TURN_MAX_MICRO_EUR` in "Fixed Trial Policy") is not
an independent `init` flag: by design it is always set equal to whichever cutoff
was chosen (EUR 40 for `--cutoff-eur 40`, EUR 95 for the default), so the
ledger's own cutoff/ceiling stay the only limits a turn can actually hit live,
exactly as before. `agenttalk gateway status` prints the resolved envelope for
the running install (`trial_cutoff_micro_eur`, `soft_stop_micro_eur`,
`external_ceiling_micro_eur`, and, once the child-turn cap feature is ready
(`child_cap_ready: true` - already the case right after a fresh `init`),
`child_turn_max_micro_eur`) alongside the existing `policy_hash` field.

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

### The task runs without a console window

The Scheduled Task starts the gateway with `pythonw.exe`: the same Python, from
the same folder as the `python.exe` that ran `task-install`, but without a
console window. A task tied to a console window ends when that window closes. On
2026-10-08 a Windows Terminal update closed its windows, the gateway task ended,
its automatic restart did not bring it back, and the gateway stayed down for 16
hours.

- `task-install` refuses, naming the folder, when `pythonw.exe` is missing
  next to `python.exe`.
- Run agenttalk with the runtime's `python.exe` (or `pythonw.exe`). Another
  launcher, such as `python3.exe`, has no windowless twin: `task-install` and
  `gateway start` refuse it, and `gateway status` reports
  `task_launcher_unsupported`, each with the steps to move to `python.exe`.
- LiteLLM also starts without a window.
- `pythonw.exe` has no output streams, so `gateway run` writes its own messages
  to `%LOCALAPPDATA%\agenttalk-ovh\gateway\gateway.log`, next to the LiteLLM
  log. The log is set up before agenttalk loads the gateway code, so a failure
  while loading is written there too.

To update a task installed before this change, run:

```powershell
agenttalk gateway stop --timeout 30
agenttalk gateway task-install
agenttalk gateway start
agenttalk gateway status
```

Run them with the Python runtime that installed the task, after updating
agenttalk in that runtime: the task is tied to that runtime's folder. A task
registered from another runtime folder is still refused as foreign. In that
case, stop the gateway with the runtime that registered the task, then
unregister the old task before `task-install`, as steps 1 and 3 of the upgrade
path in `docs/STEP-ENVELOPE-SERVICE-READERS.md` describe.

Until you do, `gateway status` lists `task_console_launch` among its errors
and shows these steps in `task_update`; status itself changes nothing.
`gateway start` refuses the old task and names the same steps. `gateway stop`
still accepts it, whether you run agenttalk with `python.exe` or `pythonw.exe`.

`task-install` replaces the old task only under these conditions:

- `gateway stop` has run, so its stop switch is in place. Every launch checks
  that switch before it starts, so a launch of the old task can only refuse.
- The gateway is not serving.
- The old task is exactly what agenttalk installed, apart from its console
  launch. A setting the stored task leaves out counts as the default the Task
  Scheduler schema documents for it; any other difference counts as a change.
  A task changed since then, for example to run with the highest privileges or
  with another action, trigger or setting, is refused and left as it is. To
  replace such a task anyway, check it first, then unregister it after `gateway
  stop` and run `task-install`.

It also ends any launch of the old task, before and after the replacement, and
asks Task Scheduler to confirm that none is left. Before each of these steps it
looks at the task again, and it stops, having ended nothing more, if the task
has changed. If Task Scheduler cannot confirm that no launch is left, it
refuses:

- Before the replacement, nothing has been changed.
- After the replacement, it says so. Run `gateway stop` and `task-install`
  again, and `task-install` finishes the check before it reports success.

## Linux Host

The gateway runs on a Linux host too, behind the exact same `agenttalk
gateway ...` CLI verbs used above. The service backend is selected
automatically by `sys.platform` - there is no separate Linux CLI or flag.
Instead of a Windows Scheduled Task, `task-install`/`start`/`stop` drive a
systemd **user** unit at
`~/.config/systemd/user/agenttalk-qwen-gateway-<id>.service` (the same
project-identity digest the Windows task name already uses) via
`systemctl --user enable|start|stop`. The secret directory is unchanged:
`default_secret_dir()` already resolves to `~/.local/share/agenttalk-ovh` on
Linux (`LOCALAPPDATA` is unset, so it falls back to `~/.local/share`) - put
the OVH key at `~/.local/share/agenttalk-ovh/api_key.txt` there instead of
under `%LOCALAPPDATA%`.

A systemd **user** unit only runs while its user has an active login session
(or a lingering one). This project deliberately does not run
`loginctl enable-linger` for the operator - that is a host-level decision
with its own effect on the account beyond this gateway, so it is documented
here, not executed by any `agenttalk` command:

```bash
loginctl enable-linger "$(whoami)"
```

Run that once on a fresh VM, as the same user that will run the gateway, if
the gateway should stay up across logout/reboot without a live session.

### Fresh VM install

Exact operator commands for a fresh Ubuntu VM, using a project-local
virtualenv and a `--cutoff-eur 40` envelope (see "Envelope at Init" above)
so this VM's own spend stays inside its own EUR 40 share of the operator's
overall allocation:

```bash
python -m venv .venv
.venv/bin/pip install /path/to/agenttalk-*.whl
.venv/bin/agenttalk gateway init \
  --litellm-executable /path/to/litellm \
  --opening-eur 0 \
  --opening-evidence "OVH AI Endpoints dashboard, observed <date>" \
  --cutoff-eur 40
.venv/bin/agenttalk gateway cap-install
.venv/bin/agenttalk gateway task-install
.venv/bin/agenttalk gateway start
.venv/bin/agenttalk gateway status
```

`cap-install` (documented under "Spend and Failure Semantics") is included
above for parity with an existing schema-v1 ledger being migrated onto a new
host; a fresh `init` already creates a ledger with the child-turn cap feature
active (`gateway status`'s `child_cap_ready` is `true` immediately after
`init`), so on a genuinely fresh VM install it is a harmless, idempotent
confirmation, not a required activation step. Everything after `init`
(`cap-install`, `task-install`, `start`, `status`, `stop`, `reconfigure`,
`runtime-rebind`, `hold`/`clear-hold`, `reconcile`, `canary-verify`) is
identical to the Windows walkthrough above; only the underlying service
backend differs.

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

## Quota lease binding (child-cap schema 4)

In plain words: the gateway can now tie a paid child turn to a reference from an
outside quota program, called a quota lease reference. Such a turn can carry its
own smaller caps. When it ends, the ledger keeps a permanent receipt of what it
spent. Nothing changes until you choose: an existing ledger works exactly as
before until you migrate it, and after that the gateway still accepts turns
without a reference until you turn the binding flag on. This version adds the
gateway side only; the wrapper does not send references yet, so every child turn
it opens stays unbound.

A turn with a reference can set three caps: the number of calls, the money in
micro-euro, and how long it may stay open. A cap that is left out means the
ledger's own ceiling for calls and money, and **24 hours** for how long it may
stay open. No cap can be above the ledger's ceiling, and 24 hours is also the
longest a turn may stay open. Caps without a reference are refused.

What you will notice:

- A fresh `agenttalk gateway init` creates the new ledger shape, child-cap
  schema 4 on ledger schema 3, with the binding flag off. Older agenttalk
  versions refuse such a ledger completely.
- An existing ledger stays on schema 3 and behaves as before. Calls that use a
  reference are refused there until you migrate.
- After the migration, `agenttalk gateway status` shows five new counts under
  `ledger` (doctor's JSON shows the same, because it carries that output).
- After the migration, a running gateway reports `runtime_marker_invalid` until
  you start it again, because the child-cap policy hash changed. The steps
  below keep the gateway stopped for the whole upgrade, so you only see this if
  you skip that.
- On schema 4, a child turn that has ended keeps its first ending. Older code
  could change a turn stopped by its call ceiling to `expired` once its time
  also ran out.

### Upgrade an existing ledger

The migration has two steps. The database step is one transaction: it either
completes or leaves the database exactly as it was. Then the install marker
(`install.json`) is updated. If that second step fails, the database is
upgraded but the marker is not; every agenttalk version, older or newer,
refuses that state, and running `binding-install` again finishes it. The
migration also moves the ledger schema from 2 to 3, and older
agenttalk code refuses every operation on a ledger at schema 3: not only its
status, but every reservation, settlement, reconciliation, hold and child turn.
So **upgrade the gateway's runtime before you migrate**. An older gateway left
running would stop working against the migrated ledger.

1. Stop the gateway and keep it stopped until step 7:

   ```powershell
   agenttalk gateway stop --timeout 30
   ```

2. Back up the ledger: copy the whole `%LOCALAPPDATA%\agenttalk-ovh-spend`
   folder (the ledger and its install marker) somewhere safe.
3. Install this agenttalk version everywhere the gateway runs from, including
   the runtime its scheduled task starts.
4. Resolve every open provider attempt. The migration refuses while any
   attempt is unresolved. `agenttalk gateway status` lists them under
   `ledger.unresolved`; reconcile each one from provider evidence:

   ```powershell
   agenttalk gateway reconcile ATTEMPT_ID --outcome no-send --reason "provider confirms no request"
   agenttalk gateway reconcile ATTEMPT_ID --outcome charge-reserve --reason "charge remains uncertain"
   ```

5. Migrate. The command reads the front token from its usual file; you never
   type a token:

   ```powershell
   agenttalk gateway binding-install
   ```

   It prints `"installed": true`, `"schema_version": 4`, the new
   `policy_hash` and `"quota_lease_binding_required": false`. A second run
   prints `"installed": false` and changes nothing. If it stops with an error
   after the database changed (for example, the install marker could not be
   written), every agenttalk version refuses the ledger until you run
   `binding-install` again; that run finishes the job.
6. Check the result: in `agenttalk gateway status`, `ledger.schema_version` is
   `3`, `ledger.child_cap_schema_version` is `4` and
   `ledger.child_receipt_report_version` is `2`. The command exits non-zero
   while the gateway is stopped; that is expected here.
7. Start the gateway. Start records the new policy hash:

   ```powershell
   agenttalk gateway start
   ```

To roll back before any new activity, keep the gateway stopped, restore the
folder you copied in step 2 and reinstall the older version. Restoring that
backup is allowed only while nothing new has been recorded since it was made:
no provider activity, no new binding and no receipt.

Once new activity exists, do not restore the older ledger in place, and do not
start older code against the updated ledger (it refuses it anyway). Keep the
updated ledger, and use a
forward repair or an explicitly reviewed recovery plan that keeps every
liability and the receipt history.

### The binding flag

To require a reference for every new child turn:

```powershell
agenttalk gateway binding-required --on
```

`--off` turns it off again. The command prints the new value as
`quota_lease_binding_required`, and `changed` says whether it was different
before. It reads the front token from its usual file, like `binding-install`,
and is refused on a schema-3 ledger.

What the flag does: while it is on, the gateway refuses to open a child turn
that has no reference. The wrapper treats that refusal as a hold, so the
message waits and is retried; it is not dead-lettered. **This version's wrapper
sends no references, so turning the flag on pauses every paid Qwen turn until
you turn it off.**

What the flag does not do:

- It gives admission control only. It is not a per-attempt authority check and
  not a sized reservation.
- It never removes an existing binding and never switches off a cap. The caps of
  a bound turn apply with the flag on or off, and can only be equal to or lower
  than the ledger's own ceilings.
- Installing this version, or turning the flag on, is not activation of complete
  paid-quota enforcement. Live activation and the gateway upgrade stay separate
  operator decisions, each with its own tested prerequisites.
- In this version, live use keeps the ledger's existing money ceiling. Smaller
  per-turn money caps are exercised in tests only, until a later sized
  reservation is approved.

### Receipts

When a child turn with a reference ends, the ledger writes one permanent
receipt. It holds the SHA-256 of the reference (never the reference itself), the
outcome (`completed`, `cancelled`, `failed` or `provider_limit`), the number of
calls, the tokens, the cost in micro-euro and the close time. A page also shows
each receipt's month, `charge_period` (see "The ledger report" below). A receipt is
written only after every provider attempt of that turn is resolved. Until then a
pending note waits, and the attempts keep their full reservation.

To read the receipts, ask for the ones after a number you already have:

```powershell
agenttalk gateway receipts --after 0 --json
```

Example output (from a test ledger):

```json
{"after_seq":0,"envelope_version":1,"generation":"0123456789abcdef0123456789abcdef","has_more":false,"next_seq":1,"receipts":[{"actual_micro_eur":1290,"calls":1,"charge_period":"2026-10","closed_at":"2026-10-03T10:03:00.000000Z","input_tokens":1200,"outcome":"completed","output_tokens":300,"quota_lease_ref_sha256":"d8b85cf28e9743fe6abc53d28753a832f037530e484455cdff76f473d69b5433","seq":1}]}
```

- `--json` is required. `--limit` takes 1 to 1000 (default 100); `has_more`
  says the limit cut the page, and `next_seq` is the number to pass as `--after`
  next time.
- Receipt numbers start at 1 and have no gaps. A reader treats a gap as damage.
- The command needs no agenttalk project: it reads only the per-user ledger
  and the front token, so you can run it from any folder.
- When its arguments parse, any failure prints nothing on standard output,
  one word on standard error (`bad_request`, `receipt_page_refused` or
  `receipts_unavailable`), and exits with code 2. It never prints a path or
  any other text.
- An option the command does not know, or an option given without its value,
  is an argument error: like every agenttalk command, it prints the usual usage
  text and exits with code 2. An interrupted run prints `agenttalk:
  interrupted` and exits with code 130.

`agenttalk gateway status` shows, under `ledger`:

- `child_receipt_report_version`: `2` (every receipt row carries
  `charge_period`);
- `child_receipts`: the number of receipts;
- `child_receipts_pending`: pending notes still waiting for an attempt;
- `child_receipts_fallback`: referenced turns whose ending was filled in with
  the fallback rule. Nothing in agenttalk can produce such a turn (see the
  limits below), so this reads `0` unless someone edited the ledger by hand;
- `child_receipts_through_seq`: the highest receipt number this status covers,
  `0` if none. It is read in the same snapshot as the money totals, so it never
  counts a receipt whose cost is missing from them. The number only means
  something together with the ledger's `generation`.

### The ledger report (report version 1)

In plain words: `agenttalk gateway report` prints the ledger's money figures,
all read at one moment, as JSON. It is meant for a program that keeps its own
accounts of what quota leases spent. It reads only the per-user ledger: it needs
no agenttalk project and no token, and it checks nothing else on the machine (no
scheduled task, port or token file). It never changes the ledger, never ends a
turn, and never names an agent, a message, an attempt or a reference. With the
receipt pages, a reader can count every micro-euro exactly once.

```powershell
agenttalk gateway report --json
```

Example output (from a test ledger; the command prints it on one line, without
spaces). One call that belongs to no lease cost 670, a finished turn has receipt
1 for 1290, and an open turn has spent 670 so far:

```json
{
  "child_cap_policy_hash": "c5172fff7c21896306d2fae47ec310105e1c3b7ff4cf9e73a5ff3b1e61ce5517",
  "child_cap_ready": true,
  "child_receipt_report_version": 2,
  "child_receipts_pending": 0,
  "child_receipts_through_seq": 1,
  "earliest_open_expiry": "2026-10-04T10:05:00.000000Z",
  "gateway_report_version": 1,
  "generation": "0123456789abcdef0123456789abcdef",
  "observed_at": "2026-10-03T10:06:00.000000Z",
  "open_child_turns": 1,
  "open_child_turns_expired": 0,
  "periods": [{"committed_micro_eur": 2630, "period": "2026-10"}],
  "policy_hash": "6df40ecdf2c22a9d06a73c2b2d7090b40d722d590e04237b19c903cb034dd5c2",
  "service_hold": false,
  "service_hold_reason": null,
  "unreceipted_bound_actual": [{"micro_eur": 670, "period": "2026-10"}],
  "unresolved": []
}
```

- `--json` is required.
- The command exits with code 0 whenever the ledger gives its snapshot, also
  while the ledger holds spending or has an unresolved attempt.
- Otherwise it prints nothing on standard output, one word on standard error
  (`bad_request` without `--json`, `report_unavailable` for every other
  failure), and exits with code 2. It never prints a path or any other text.
  The report fails wherever `agenttalk gateway status` cannot read the ledger:
  no ledger or half a ledger, a damaged ledger, or a clock behind the ledger's
  last recorded time. It also fails rather than print a figure outside the
  report's closed shape or bounds. A stored amount must be a whole number from
  0 to 10^12 exactly as stored: a fraction such as 1.5, a text value or a
  larger number refuses the report. It is never rounded or converted. And the
  receipts must be numbered 1, 2, 3 and so on without a gap, the same rule a
  receipt page applies, so the report never advertises a receipt number the
  pages cannot supply.
- An option the command does not know is an argument error: it prints the usual
  usage text and exits with code 2.
- **A report is never permission to spend.** It has no readiness figure. Whether
  the gateway may spend is decided by the gateway itself, at each call.

**The figures.** Money is in whole micro-euro (1 EUR is 1,000,000 micro-euro). A
period is a calendar month in UTC, written `YYYY-MM`. The ledger names every
period with one function: the rows of `periods`, an attempt's period and a
receipt's `charge_period` all come from it. An attempt belongs to the month in
which it was reserved, even when it settles later.

A **charged attempt** is one whose recorded cost is money spent: it is settled;
or reconciled as `charge-reserve`; or `uncertain` with a recorded cost (its
settlement went over the reservation). An attempt reconciled as `no-send` sent
nothing and records 0; it is not a charge.

| Figure | What it is |
|---|---|
| `gateway_report_version` | `1`. A reader refuses a version it does not know. |
| `observed_at` | The report's own clock reading, in UTC. |
| `generation` | The ledger's generation, 32 hexadecimal digits. Every receipt page names the same value. Receipt numbers only mean something within one generation. |
| `policy_hash` | The price policy hash, as `agenttalk gateway status` shows it. |
| `child_cap_policy_hash` | The child-turn policy hash, or `null` when the ledger has no child-turn feature. |
| `child_cap_ready` | `true` when the ledger has the child-turn feature. When it is `false`, every child-turn and receipt figure below is `null` and `unreceipted_bound_actual` is empty. |
| `periods` | One row per month the ledger has opened, in month order: `period` and `committed_micro_eur`. A month without a row has committed 0. |
| `unresolved` | Every attempt still `reserved` or `uncertain`, oldest first: `state`, `reserved_micro_eur`, `actual_micro_eur` (`null` until a cost is recorded) and `period`. No ids. |
| `open_child_turns` | Child turns still recorded as open, with or without a reference. |
| `open_child_turns_expired` | How many of those had reached their expiry time at `observed_at`. They stay open until something ends them: a close, or a later open or reserve of that turn. The report never does. |
| `earliest_open_expiry` | The earliest expiry time of an open turn, or `null` when none is open. |
| `child_receipt_report_version` | `2`: every receipt row carries `charge_period`. `null` when quota lease binding is not installed (child-cap schema 3, or no child-turn feature). |
| `child_receipts_through_seq` | The highest receipt number in this snapshot, `0` if none. `null` without binding. |
| `child_receipts_pending` | Ended turns with a reference whose receipt waits for an unresolved attempt. `null` without binding. |
| `service_hold` | `true` while the ledger holds new spending. |
| `service_hold_reason` | `null` without a hold. Otherwise one word: `attempt_over_reservation` (a settlement went over its reservation), `dashboard_canary_mismatch`, `manual` (an operator's hold) or `other` (any other hold). The hold's own text is never shown. |
| `unreceipted_bound_actual` | One row per month, in month order: `period` and `micro_eur`, the sum of the recorded costs of charged attempts whose child turn has a reference and no receipt yet. A month without such an attempt has no row. Always empty without binding. |

**What each month's committed money holds.** `committed_micro_eur` for a month
is the sum of the recorded costs of all its charged attempts, plus the opening
amount in the opening month only. That includes every reconciliation (a
`charge-reserve` reconciliation raises the attempt's cost and its month's
committed money by the same increment) and the recorded cost of an attempt left
`uncertain` because it went over its reservation. An `uncertain` attempt without
a recorded cost is not in it yet.

**The receipt month.** Each receipt row carries `charge_period`: the month that
every charged attempt of its turn shares. It is `null` when those attempts fall
in different months, or when the turn has no charged attempt (a receipt of 0).
It is worked out from the attempts each time a page is read; nothing new is
stored. A receipt's `actual_micro_eur` is the sum of the costs of exactly those
charged attempts, so a receipt that names a month has all its money in that
month.

**The guarantees a reader relies on:**

1. **One snapshot.** Every figure comes from one read transaction. No write can
   be committed while it lasts.
2. **One place at a time.** Until a turn with a reference has its receipt, the
   costs of its charged attempts are in `unreceipted_bound_actual`. The receipt
   is written in the same transaction as the turn's end, or as the settlement or
   reconciliation that resolves the turn's last attempt. From then on the money
   is in the receipt, for the same amount, and no longer in
   `unreceipted_bound_actual`: never in both, never in neither.
3. **Receipt order.** A receipt is numbered `MAX(seq) + 1` inside the
   transaction that writes it, which holds the ledger's writer lock, and a
   receipt is never changed or deleted. So after a report that read
   `child_receipts_through_seq` = N, no receipt numbered N or lower can appear
   later, and the receipts numbered up to N never change.

**Counting a month exactly.** To find, for month P, the money that no quota lease
accounts for, take one report and read the receipt pages:

- `covered(P)`: the sum of `actual_micro_eur` over receipts numbered up to the
  report's `child_receipts_through_seq` whose `charge_period` is P;
- `unreceipted(P)`: the `unreceipted_bound_actual` row for P, or 0;
- unowned money for P: `committed(P) - covered(P) - unreceipted(P)`.

What remains is the opening amount (opening month only), calls that belong to no
quota lease (no child turn, or a turn without a reference), and each month's part
of a receipt whose `charge_period` is `null`. In the example above, October's
unowned money is 2630 - 1290 - 670 = 670: the one call without a lease.

- Use only receipts numbered up to the report's own
  `child_receipts_through_seq`. A receipt written after the report carries money
  that report still counted in `unreceipted_bound_actual`; counting it as well
  would subtract that money twice.
- Every page must name the report's `generation`. A page with another generation
  belongs to another ledger.
- With these rules the result is never below 0. A negative result means the
  inputs do not belong together.
- A receipt with a `null` month counts in no month's `covered`. Its money stays
  in the committed money of each month it was spent in, and a reader cannot
  give it to the lease month by month.
- **This is not exact monthly lease attribution.** A cross-month receipt's
  per-month shares stay in the unowned residual although a lease owns them, so
  a consumer must keep that qualification wherever it shows the unowned figure.

### Limits of this version

- **No bounded receipt time.** Nothing in this version runs the start-up sweep
  that ends expired turns; a later open or reserve of the same turn notices the
  expiry instead. Until then there is no receipt. A missing receipt is never a
  zero.
- **Unbound turns have no receipt.** That includes a turn that was opened
  without a reference and then closed with one.
- **"closed" is only an acknowledgement.** It never proves that a receipt exists
  or that the cost is zero.
- **A receipt is not split between months.** A turn whose charges fall in two
  months gets a receipt with a `null` `charge_period`. Each part stays in its
  own month's committed money, but no month's figures can give that part to the
  lease.
- **No compaction.** Receipts and pending notes are kept for the life of the
  ledger. A rebuilt ledger cannot recreate the receipts of the old one.
- **A turn with no recorded ending stops the ledger.** An ended, referenced
  turn without a recorded ending can only come from a hand edit or a damaged
  backup. The ledger's integrity check refuses such a ledger before the sweep,
  or anything else, could repair it, so the ledger stops serving every call
  until an operator acts (for example by restoring a good backup). There is no
  automatic repair. The fallback rule (`expired` becomes `cancelled`, a call or
  cost refusal becomes `failed`) only fills in endings for turns the migration
  copies, and those carry no reference.

Technical details:

- Schema 4 adds the `child_turns` columns `quota_lease_ref_sha256`,
  `terminal_outcome`, `terminal_at` and `terminal_source`, the tables
  `child_receipts` and `receipt_pending`, and the metadata key
  `quota_lease_binding_required` (`0` or `1`; any other value blocks the
  ledger, it is never read as off). Database triggers keep an ending, a
  binding, a receipt and a pending note from ever changing, including through
  `INSERT OR REPLACE` (which skips update and delete triggers), and keep a
  bound child turn from being deleted. A partial unique index lets one
  reference bind at most one child turn.
- Child-cap schema 4 always sits on ledger schema 3, in the database and in the
  install marker; schema 3 sits on ledger schema 2. Every writer from before
  quota lease binding checks for ledger schema 2 on each connection, so it
  refuses the whole ledger. The migration also refuses a clock behind the
  ledger's own initialization or last accepted admission, not only one behind
  the child turns.
- A reference must match `^[A-Za-z0-9._:-]{1,128}$`. Only its SHA-256 is
  stored; no error, status or receipt shows the reference.
- `SpendLedger.open_child_turn` takes the optional keywords `quota_lease_ref`,
  `max_calls`, `max_micro_eur` and `ttl_seconds`. With a reference, a null
  cap means the ledger ceiling and a missing `ttl_seconds` means 24 hours
  (86 400 seconds); caps or `ttl_seconds` without a reference are refused. A
  retry must bring the same values. `close_child_turn` takes the optional
  `outcome` and `quota_lease_ref`; closing a bound turn needs both. Called with
  either of them, it returns one word: `closed`, `already_terminal`, `fenced`,
  `reference_ignored` or `not_opened`; a call without them returns `None`, as
  before. A close with a reference
  for a key that never opened writes a `fenced` turn and a zero receipt, so that
  reference can never open later.
- The read methods are `SpendLedger.child_receipts_page`,
  `quota_lease_binding_state`, `status` and `report`; none of them writes.
  `report` checks its own result with `check_gateway_report`, the report's
  closed shape, before it returns it; `check_receipt_page` does the same for a
  page. A receipt's `charge_period` and the report's
  `unreceipted_bound_actual` use the one charged-attempt rule (`_charged`). The start-up
  sweep is `SpendLedger.sweep_child_receipts`.
- The three commands call `install_child_cap_binding_as_operator`,
  `set_quota_lease_binding_required_as_operator` and
  `child_receipts_page_as_operator`. These read the front token from its usual
  file inside the ledger code, so the command itself never holds it.

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
