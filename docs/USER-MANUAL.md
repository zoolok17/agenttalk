# agenttalk user manual

Audience: the human operator running agenttalk for a local coding-agent pair
or team.

Goal: get a team initialized, keep the bus healthy, route work, inspect
obligations, and understand the optional supervisor, lane, knowledge, and
assurance layers without reading the maintainer design docs first.

agenttalk is local-first and file-backed. It assumes a trusted workspace where
the operator and rostered agents can read and write the project. Roles, lanes,
gates, lead-chat, and operator metadata are coordination and audit tools, not
enterprise authorization, not a security sandbox, and not magic autonomy. Git,
the OS, and your review process remain the real boundaries.

For architecture, release attestations, issue tracking, and roadmap context,
see:

- [DESIGN.md](DESIGN.md)
- [ASSURANCE.md](ASSURANCE.md)
- [ISSUES.md](ISSUES.md)
- [ROADMAP.md](ROADMAP.md)
- [SECURITY.md](../SECURITY.md)
- [CHANGELOG.md](../CHANGELOG.md)

## 1. What agenttalk is for

agenttalk is a small message bus and coordination layer for coding-agent CLIs
such as Claude Code and Codex. It lets agents in separate terminal windows talk
directly while working in the same repository. The operator stops copy-pasting
between windows and instead assigns work, reads evidence, and makes decisions.

Every project has one `.agenttalk/` directory. Messages, cursors, heartbeats,
lanes, gates, knowledge notes, and close records are durable files under that
project. There is no server or database to operate.

Use agenttalk when you want:

- one implementer and one reviewer to coordinate without a human relay;
- a named team with a human-facing lead or liaison;
- durable thread state after an agent or terminal restarts;
- optional local dashboard visibility;
- optional unattended supervision with heartbeat-based liveness;
- optional lanes, gates, and close records for higher-assurance work.

Do not treat agenttalk as:

- a malicious-peer defense inside an untrusted repository;
- per-agent cryptographic identity;
- a remote multi-user web app;
- an autonomous project manager that decides correctness for you.

The product value is disciplined coordination. A second agent agreeing with the
first is not enough; executable evidence, fresh review, and explicit operator
decisions are what make a GO credible.

## 2. Install and initialize

Install once per machine. Pin the version in repeatable setups.

```powershell
python -m pip install git+https://github.com/zoolok17/agenttalk.git@v0.81.0
agenttalk --version
agenttalk --help
agenttalk install-skills
```

If you run commands from inside a Codex sandbox and bare `agenttalk` is denied,
use the module form:

```powershell
python -m agenttalk --help
```

Initialize once per project from the repository root:

```powershell
agenttalk init --here --agents 'claude,codex'
agenttalk status
agenttalk doctor
```

The roster must contain at least one agent. You can initialize a project for a
single Claude or Codex terminal and add another roster member later:

```bash
agenttalk init --here --agents claude
agenttalk init --here --agents codex
```

With a single-agent roster, set only `AGENTTALK_SELF`; do not set
`AGENTTALK_PEER` until another agent has been added. Messaging commands that
target a peer require an explicit `--to` (or a peer in the roster).

For Codex, enable the project sandbox block so Codex can call agenttalk from
inside its workspace sandbox:

```powershell
agenttalk codex-config --enable
```

On Windows PowerShell, set the current terminal identity like this:

```powershell
$env:AGENTTALK_SELF = 'claude'
$env:AGENTTALK_PEER = 'codex'
```

On bash or zsh:

```bash
export AGENTTALK_SELF=claude
export AGENTTALK_PEER=codex
```

The `.agenttalk/` directory lives in the project root you initialized. When you
work from another directory, put the global `--root` option before the
subcommand:

```powershell
agenttalk --root D:\Projects\example sync --for claude
```

`agenttalk init --force` overwrites project config, not messages, cursors, or
heartbeats. Use `agenttalk reset` only when you intentionally want to clear
active bus state.

## 3. First two-agent workflow

Start with one terminal per active agent. Keep one live consumer per agent
mailbox. Do not run two listen loops for the same roster name.

1. Initialize the project:

   ```powershell
   agenttalk init --here --agents 'claude,codex'
   ```

2. In the Claude terminal:

   ```powershell
   $env:AGENTTALK_SELF = 'claude'
   $env:AGENTTALK_PEER = 'codex'
   agenttalk whoami --for claude
   ```

3. In the Codex terminal:

   ```powershell
   $env:AGENTTALK_SELF = 'codex'
   $env:AGENTTALK_PEER = 'claude'
   agenttalk whoami --for codex
   ```

4. Send a first message:

   ```powershell
   agenttalk send --from claude --to codex --kind question --subject first-check --meta request_id=q-first-check -m "Can you see this?"
   ```

5. Read and consume it from the other terminal:

   ```powershell
   agenttalk drain --for codex
   ```

   `recv --for codex` is a peek: it prints unread messages but does not move
   Codex's cursor. Use `drain --for codex` or `recv --for codex --ack` when
   you want to consume the message.

6. Reply on the same request thread:

   ```powershell
   agenttalk reply --from codex --to-request q-first-check -m "Yes."
   ```

7. Read and consume the reply from the Claude terminal:

   ```powershell
   agenttalk drain --for claude
   ```

8. Rejoin and inspect state whenever a terminal restarts:

   ```powershell
   agenttalk sync --for claude
   agenttalk threads --for claude
   agenttalk status
   ```

For real agent windows, you normally tell the agents to use their installed
agenttalk skills. The operator still owns roster, assignment, and release
decisions.

## 4. Roster, identity, roles, and the operator-facing lead

An agent name is a safe identifier: alphanumeric plus dot, underscore, or
hyphen, starting with an alphanumeric. Use distinct names such as
`claude-dev`, `codex-dev`, `claude-rev`, and `codex-rev` for teams.

Roster commands are deliberate local admin operations:

```powershell
agenttalk roster
agenttalk roster add claude-dev --role implementer --group devs
agenttalk roster add codex-dev --role implementer --group devs
agenttalk roster add claude-rev --role reviewer --group reviewers
agenttalk roster add codex-rev --role reviewer --group reviewers
agenttalk roster add claude-lead
agenttalk roster set-role claude-lead lead
agenttalk roster set-group reviewers 'claude-rev,codex-rev'
agenttalk roster set-operator-facing claude-lead
agenttalk roster --json
```

Designate one operator-facing liaison:

```powershell
agenttalk roster set-operator-facing claude-lead
```

That liaison is the single agent the human talks to directly. Escalations route
to it. If no liaison is configured, some commands fall back to the sole active
`role=lead`. If there is no sole lead, or multiple active leads, the command
fails closed and tells you how to fix the roster.

Common mistakes:

- Relying on default `claude` and `codex` names in a larger team. Set
  `AGENTTALK_SELF` or pass `--from` and `--for` explicitly.
- Trusting a roster you copied into a doc, memorized, or received in a handoff.
  Membership, roles, and the operator-facing liaison change over time; re-check with
  `roster`, `status`, or `sync` at the moment you act, not from a cached snapshot.
- Removing an agent with history. Prefer `roster retire`; it keeps history
  readable and prevents unsafe name reuse.
- Treating `operator_facing` as security authority. It is routing metadata and
  audit context in a trusted workspace.
- Leaving no operator-facing liaison when workers may need human decisions.
  `doctor` warns about this when escalation traffic exists.

## 5. Bus basics: send, reply, threads, sync, and status

Use `send` for a new point-to-point message:

```powershell
agenttalk send --from claude-dev --to codex-rev --kind review-request --subject review-cli -m "Please review the CLI change."
```

Kinds such as `question`, `review-request`, and `proposal` are tracked by
request id. If you do not provide `--meta request_id=...`, agenttalk mints one
and prints it.

Use `reply` for the answer:

```powershell
agenttalk reply --from codex-rev --to-request <request-id> --kind review-result --meta status=rejected -m "REQUEST-CHANGES: missing test."
```

Use `threads` to see who owes the next move:

```powershell
agenttalk threads --for claude-dev
agenttalk threads --for claude-dev --all
agenttalk threads --for claude-dev --json
```

Useful thread states:

- `owed-inbound`: the peer is waiting on you.
- `open-outbound`: you are waiting on the peer.
- `reply-waiting`: a response is in your inbox but not consumed.
- `closed`: the thread has a terminal response or local closure.
- `closed-superseded`: the opener rescinded the request.

Typed response status is strict:

- `review-result`: `approved`, `rejected`, or `needs-info`; only approved and
  rejected close the review. `needs-info` returns the obligation to the
  requester.
- `proposal-response`: `accepted`, `rejected`, or `countered`; all three are
  terminal for that proposal.

A missing status remains readable in old history but does not close a thread.
An invalid present status is rejected on send/reply and skipped if encountered
in persisted input.

Use `sync` after every restart or before taking over a stale window:

```powershell
agenttalk sync --for claude-dev
agenttalk sync --for claude-dev --lesson-tag docs
```

`sync` gives identity, roster, open threads, terminal decisions, unread FYI
traffic, and a capped Lessons to check section when accepted lessons match the
work. Wrapped agents get matching lessons injected by the wrapper instead of
running `sync`; do not ask a wrapped child model to run inbox/cursor commands.

Use `status` for a team-wide snapshot:

```powershell
agenttalk status
agenttalk status --json
```

Use `recv` to peek and `drain` to consume:

```powershell
agenttalk recv --for codex-rev
agenttalk drain --for codex-rev
agenttalk drain --for codex-rev --limit 5
```

Use `--limit` (or `-n`) when you want a bounded consuming page. Do not
truncate an unbounded `drain` or `recv --ack` with a downstream command such
as `head` or `Select-Object -First`; agenttalk refuses an unbounded consuming
read when stdout is a pipe so undisplayed mail cannot be marked consumed.
Unbounded output to a terminal or regular file remains supported.

Use `wait` for live listening:

```powershell
agenttalk wait --for codex-rev --timeout 1800
agenttalk wait --for codex-rev --to-request <request-id> --kind review-result
```

If `wait --refuse-stacked-wait` exits 6, another live waiter already owns that
mailbox. Stop the duplicate; one live consumer per agent is the supported rule.
If an older scoped wait exits 6 with `superseded` on stderr, a newer wait for
the same request replaced it; the older wait did not consume a message or move
the thread cursor.

Managed-wrapper consult and handoff skills do not start a child `wait`. They
send their tracked opener with `--await-reply`, record a generation-bound,
body-free wait token, and return to the wrapper that owns the inbox cursor. The
wrapper delivers the correlated response in a later turn. Only the bundled
consult and handoff skills opt in; ordinary `send`/`reply` and every other skill
behave as before. To abandon an explicit wait while its token is still current,
run `agenttalk await-cancel --from <agent> --token <await_reply_token>`.

## 6. Running a lead and a team

The lead coordinates work, but does not become an authority boundary. A lead's
prose does not close threads, stand agents down, override gates, or prove a
release safe. Use typed commands for state changes.

Basic lead cadence:

1. Rejoin:

   ```powershell
   agenttalk sync --for claude-lead
   agenttalk threads --for claude-lead
   agenttalk status
   ```

2. Assign work with point-to-point messages or broadcasts:

   ```powershell
   agenttalk send --from claude-lead --to codex-dev --kind question --subject bugfix -m "Build the narrow bugfix and report SHA plus tests."
   agenttalk broadcast --from claude-lead --to-group reviewers --kind question --subject review-plan -m "Who can review the final SHA?"
   ```

3. Track replies:

   ```powershell
   agenttalk threads --for claude-lead
   agenttalk wait --for claude-lead --to-request <request-id>
   ```

4. Route human decisions through the liaison path. Workers should use
   `escalate`, not ask their own terminal:

   ```powershell
   agenttalk escalate --from codex-dev --subject operator-needed -m "Need approval to delete generated artifacts."
   ```

Avoid copy-paste coordination. If a worker needs a reviewer, use a bus
assignment or the relevant skill. If a human decides something, relay it
through a typed command rather than burying it in prose.

## 7. Dashboard and lead chat

The dashboard is local and loopback-only. It is for the operator at the same
machine, not for remote multi-user access.

Start the obligation dashboard:

```powershell
agenttalk dashboard
agenttalk dashboard --port 0
agenttalk dashboard --store D:\Projects\project-a --store D:\Projects\project-b
```

Start the message browser view:

```powershell
agenttalk serve
```

`dashboard` has no `--host` option. `serve` accepts only loopback hosts:
`127.0.0.1`, `::1`, or `localhost`.

The top bar always shows selected-project and path context. If CSS ellipsizes
the path visually, its complete value remains available through the element
text, title, and accessibility label. In a multi-root dashboard, the
path-derived `project_id` is the routing identity; labels are display-only for
writes. Selected-root responses return `root_info` containing id, label, and
path. GET may omit `root` to select `root[0]` and may use a unique display label
as a legacy best-effort selector. Blank, repeated, unknown, or ambiguous GET
selectors return HTTP 400 `bad_root`.

Mutating POST `/api/intent` and `/api/lead-chat` are stricter: when multiple
roots are served, they require exactly one explicit full
`?root=<project_id>`. They never accept a label, and an omitted selector is
accepted only with one root.
Unknown, blank, repeated, ambiguous, or non-full selectors return HTTP 400
`bad_root` before anything is written.

Changing the selector to a different project pushes its id into browser
history. Back and Forward restore the selected project and refetch its
root-bound feeds. Any actual project change clears thread drill-ins, caches,
the action session, queued-answer text, and generic and lead-chat composer
drafts. Responses from
the old project are ignored unless their `root_info.project_id` still matches.
This prevents accidental cross-root UI actions; `project_id` is not
authentication, and any local process that can reach the loopback server can
inspect every exposed root.

Browser actions are off by default:

```powershell
agenttalk dashboard --enable-actions
```

Enable actions only in a trusted local session. The browser can enqueue typed
intents that the server validates and the supervisor drains. It cannot bypass
server-side authority checks, and it is not a cryptographic boundary against a
fully privileged local process.

Lead chat lets the local operator send a direct bus message to the configured
lead from the dashboard. It uses a reserved operator principal and local
session/CSRF checks. It does not authorize generic browser-origin messages as
agent messages.

For lead-chat reachability, the operator-facing lead must have a fresh
heartbeat. It can be active in `wait`, running under the wrapper, or refreshed
by the interactive activity hook. A stale or missing heartbeat means lead-chat
reports the lead as unavailable. That is intentional.

The **Learning** view answers "what was learned, how, and by whom" for curated
lessons. It is read-only and defaults to accepted active lessons: lesson text,
trigger, publisher, curator, owner, evidence reference, anchor metadata,
exposure count, and recent wrapper exposure pointers. Exposure means the
wrapper surfaced a matched accepted lesson to an agent turn; it does not prove
the model read, remembered, or applied it. Proposed, stale, retired, or
superseded lessons stay out of the default view and should be inspected through
explicit CLI/API filters when you are doing curation or diagnosis.

The **Onboarding** view tracks codebase-analysis runs before implementation:
segments inspected, claims recorded, open documentation/code drift, blocking
unknowns, and ledger-health warnings. It is read-only and pointer-first. It
shows bounded summaries, paths, refs, and counts from `GET /api/onboarding`;
it does not show raw bus message bodies, prompt blocks, copied source, or full
command output.

## 8. Supervisor and unattended operation

The supervisor is optional. Use it when agents need to survive unattended
provider outages, rate windows, CLI crashes, or stuck turns.

Scaffold:

```powershell
agenttalk supervise --init
```

This writes `.agenttalk/supervisor.json`, `.agenttalk/supervisor.ps1`,
`.agenttalk/supervisor-task.ps1`, `.agenttalk/deadman.ps1`, and
`.agenttalk/bin/agenttalk.cmd`.

The Windows scripts require PowerShell Core 7+. Stable 7.4+ is recommended;
stable 7.0-7.3 and prereleases run with warnings, while Windows PowerShell 5.1
is refused. Select the host once, then run the monitor through the returned
absolute path:

```powershell
$pwshPath = (agenttalk supervise --select-pwsh | ConvertFrom-Json).path
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor.ps1
```

Use `--select-pwsh --pwsh 'C:\absolute\path\pwsh.exe'` for a nonstandard
installation. That explicit candidate is terminal and never falls through to
another installation. Selection and native file identity are same-user
consistency controls, not executable signer/ACL attestation.

Read what the supervisor sees:

```powershell
agenttalk supervise --report
agenttalk supervise --plan
agenttalk supervise --bootstrap-check
```

Use `--bootstrap-check` before treating a roster as a live team. It emits JSON
and verifies the operator-facing liaison, supervisor-managed agent entries,
wrapped Claude/Codex launch invariants, explicit wrapped `--root`, filled launch
placeholders, and fresh heartbeats. A roster identity with no supervisor entry
and no fresh heartbeat is only a name, not an executing teammate; supervise it,
retire it, or deliberately ignore it before assigning work.

For durable hosting, use the generated Scheduled Task helper:

```powershell
& $pwshPath -NoLogo -NoProfile -NonInteractive -File .\.agenttalk\supervisor-task.ps1 -Action install
& $pwshPath -NoLogo -NoProfile -NonInteractive -File .\.agenttalk\supervisor-task.ps1 -Action status
```

The task freezes the selected absolute host. To change it, stop, wait for the
task and old supervisor process to exit, uninstall, select the new host, run
`agenttalk supervise --refresh-scripts`, then install and start. Task action
paths are compared as data and are never executed to discover or probe a host.

After an agenttalk upgrade, stop and wait for the claimed supervisor to exit,
then refresh the four generated files:

```powershell
agenttalk supervise --refresh-scripts
```

Refresh preserves an existing `supervisor.json` byte-for-byte and leaves
runtime state unchanged. The four replacements are individually atomic, not
group-atomic; stale/mixed sets are detected and a rerun converges. An invalid
singleton marker is recovered only with the explicit
`supervise --repair-instance-marker --quarantine
--acknowledge-no-live-supervisor` acknowledgement.

Manual listeners use heartbeat freshness as their liveness authority. Wrapped
listeners also publish a strict `wrapper-runtime.json` lifecycle record. Only a
validated idle phase can be `HEALTHY_IDLE`; active work requires an
independently discovered real CLI brain plus accepted adapter progress.
Missing, malformed, or ambiguous evidence is `CLI_CHILD_UNKNOWN`, never green
or automatic kill authority when no owned-tree HOLD applies. Owned-tree
validation runs first: an invalid or truncated tree reports
`PROCESS_TREE_INVALID` or `PROCESS_TREE_TRUNCATED`, authorizes no kill, and
leaves any restart marker unconsumed.

On Windows, `cli_launcher_lifetime` is deliberately nullable. A non-null value
is an all-or-nothing `GetProcessTimes` certificate with positive decimal
creation/exit FILETIMEs and creation before exit. Authoritative
`complete`/`absent` Windows tree entries require a positive decimal
`start_filetime`. `invalid`/`truncated` HOLD entries may retain null so their
failure evidence stays readable, but null grants no identity authority. Linux
boot-ID/start-ticks tokens are exact without FILETIME. If a prior identity
recorded an exact FILETIME, a current row with that field missing is ambiguous.
A prior complete tree bridges an exited intermediate process only for the same
wrapper generation and launch nonce, with the exact previously recorded child
identity and parent edge. A new or reparented child invalidates the tree.

### Recover an owned-process-tree HOLD

Use this attended sequence when an upgrade or a tree over the 64-entry cap
creates a nondismissible `process_tree_hold`. The reset command revokes stale
evidence only; it never stops or launches a process.

1. Create or leave `.agenttalk/supervisor.kill` in place. Stop the supervisor
   and confirm its strict instance marker is absent.
2. Read the current item with `agenttalk attention`. Record its
   `source_hash` and launch nonce.
3. Inventory and stop the complete wrapper tree. Verify every recorded
   PID/start identity is absent or definitely recycled. Before stopping the
   wrapper, re-read `--supervisor-launch-nonce` from its live command line and
   verify it matches the recorded nonce. If the wrapper is no longer live
   enough to re-read it, do not use the reset command; use manual repair.
4. As the configured operator-facing liaison (or sole lead), run:

```powershell
agenttalk supervise --reset-process-tree-ownership --from <liaison> `
  --for <agent> --hold-source-hash <64hex> `
  --verified-launch-nonce <verified-launch-nonce> `
  --acknowledge-no-live-supervisor `
  --acknowledge-owned-processes-stopped `
  --reason "attended owned-tree migration"
```

The command uses only the canonical supervisor state and rechecks the kill
switch, absent instance marker, current nondismissible Attention hash, recorded
nonce, a valid strict runtime record that agrees on wrapper
PID/start/generation, and every recorded PID/start under the lifecycle and
config locks. A stale hash, missing or mismatched nonce, invalid/mismatched
runtime record, live or unverifiable identity, or unauthorized actor refuses
the reset. If the HOLD lacks nonce/reset evidence, manual state repair remains
required. The reset atomically records the exact retired runtime digest and
PID/start/generation/nonce boundary. Only that unchanged sidecar is ignored
while the restart is queued; a changed or new-generation runtime still follows
normal fail-closed adoption.

5. Keep the supervisor host stopped, remove `supervisor.kill`, and run
   `agenttalk supervise --refresh-scripts` to regenerate and validate the
   generated artifacts. (`--refresh-scripts` refuses while the kill switch is
   present.) Queue `agenttalk request-restart --for <agent>`, then resume the
   supervisor. The next launch must earn a fresh wrapper generation and
   complete tree before automatic teardown is available.

Freshness is bounded against clock error: a heartbeat farther in the future
than the configured allowance cannot make an agent healthy. The monitor's
`supervisor-state.json` has a validated `.bak`; a corrupt primary can be read
from the backup without silently rewriting it, while two invalid copies stop
planning and action rather than resetting session state.

Manual windows plus hook:

```powershell
agenttalk supervise --install-activity-hook
agenttalk supervise --install-activity-hook --codex
agenttalk supervise --install-activity-hook --interactive-for claude-lead
```

The Claude installer writes three project hooks to `.claude/settings.json`:
heartbeat on `PostToolUse`, checkpoint save on `PreCompact`, and checkpoint
resume on `SessionStart` with matcher `compact`. `--codex` additionally writes
only the heartbeat hook to `.codex/hooks.json`; it does not install Codex
checkpoint hooks. Use the neutral hooks for supervised/manual agents launched
with `AGENTTALK_SELF`. Use `--interactive-for <lead>` only for the current
operator-facing human Claude liaison window. It gives all three Claude hooks a
fallback identity for a window without `AGENTTALK_SELF`; the environment
identity still takes precedence. Non-liaison windows should set
`AGENTTALK_SELF` instead.

### Checkpoint-before-compact reference

Automatic context compaction can omit working state. Checkpoint-before-compact
captures deterministic external anchors immediately before compaction and
re-injects a summary when the compacted session starts. It records current
context capacity, Git state, and bounded bus obligations; it cannot capture
model reasoning that exists only in the conversation.

| Command | Behavior |
| --- | --- |
| `agenttalk checkpoint save [--for A] [--trigger auto\|manual]` | Save a checkpoint directly. Non-hook saves default to trigger `manual`. |
| `agenttalk checkpoint resume [--for A]` | Render the latest checkpoint as human-readable resume context. Missing checkpoints are not errors. |
| `agenttalk checkpoint show [--for A] [--json]` | Inspect the latest checkpoint; `--json` emits its stored payload. |

The installed Claude hooks use these hook modes:

- `PreCompact` runs `checkpoint save --hook`. It reads the bounded hook payload
  from stdin, stays silent, swallows every internal failure, and **always exits
  0**, so the command cannot block a compaction.
- `SessionStart` with matcher `compact` runs `checkpoint resume --hook`. It
  always emits exactly one JSON envelope on stdout:

  ```json
  {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
  ```

  Claude injects `additionalContext` into the resumed session. When no valid
  checkpoint exists, the same envelope contains an empty context.

The latest checkpoint is
`.agenttalk/checkpoints/<agent>.json`. Each save replaces that file and keeps
up to ten prior snapshots under
`.agenttalk/checkpoints/history/<agent>/`. `agenttalk reset` deletes checkpoint
state; `agenttalk reset --archive` moves it with the prior session.

Hook identity resolves from an explicit `--for`, then `AGENTTALK_SELF`, then
the installer's `--fallback-for`. A wrapped Claude agent therefore checkpoints
as itself through `AGENTTALK_SELF`; the fallback binds only an interactive
window that lacks that environment variable.

For rollout safety, each installed checkpoint command falls back to the silent
heartbeat hook when the `checkpoint` subcommand is unavailable. A bounded
legacy-PATH residual remains: the neutral heartbeat fallback requires roughly
agenttalk v0.31.1, and the `--fallback-for` form requires v0.69.6. An older
executable selected first on `PATH` can still return exit 2; upgrade or correct
`PATH` before installing these hooks.

Recommended unattended path:

```powershell
agenttalk wrap --for codex-dev --cli codex --loop -- codex -a never -s workspace-write -C D:\Projects\example
```

Each wrapper idle marker has a unique wait token. An exiting old wrapper clears
the marker only while that token still matches, so it cannot erase a newer
wrapper's waiting state.

An explicit wrapped reply wait is active only while its wrapper generation is
current, the waiter is freshly idle, and its validated outbound thread remains
open. A missing, torn, or generation-mismatched marker fails quiet. Generic
wrapper idleness and ordinary open outbound requests do not imply a stall.

On Windows, the turn watchdog terminates a verified target with
`os.kill(pid, signal.SIGTERM)` and does not start `taskkill.exe`. Windows maps
that call to abrupt termination, not graceful signal handling. This eliminates
the popup-producing `taskkill.exe` subprocess path. The production reporter's
desktop-heap exhaustion diagnosis is plausible, not an upstream-confirmed root
cause. Windows snapshot and start-time helpers launch CIM through the selected
absolute Core host; a missing, changed, expired, or unreadable selection yields
no snapshot and therefore no kill. PID reuse remains possible after the
recheck, and snapshot-based leaf-first termination is not an atomic tree kill. Treat those limits as
follow-up hardening, not blockers for this narrow fix.

In supervisor config, wrapped agents use Python as `windows_file`; the real CLI
goes after `--` in `windows_args`. See
[supervisor-tutorial.md](supervisor-tutorial.md) for complete config examples.

### Per-agent model and reasoning effort

Each wrapped agent can pin a `model` and `reasoning_effort` in `supervisor.json`
(v0.75.0); the wrapper injects them and fingerprints the *effective* (post-injection)
value. Three layers resolve it, highest first: an explicit model/effort in the child
command after `--` (the raw launch tail) beats a `wrap --model`/`--effort` wrapper option,
which beats `supervisor.json`. Changing it starts a clean conversation only when a prior
baseline is present and the effective value actually changes — the first/absent baseline is
adopted without a reset, and a config or `wrap` value a higher layer already sets is a
no-op. The dashboard contact card shows each agent's
last-recorded effective CLI, model, and effort (it can persist while the agent is down),
plus a read-only Skill row on the profile (v0.75.1).

Configure a STABLE profile per role, not per task — because a change to the effective
value resets the session, churn costs context and latency for little gain. As a rule of
thumb:

- **Routine listeners, relays, acks:** cheap and fast (Claude `haiku` · low; Codex low).
- **Builders / implementers:** a mid model at medium/high effort (Claude `sonnet`; Codex
  medium).
- **Reviewers, architects, and the lead's design/gate/release turns:** a strong model at
  high effort (xhigh for security or release) — and for an INDEPENDENT review prefer a
  different model family than the builder's. Note an *interactive* lead is configured in
  its own CLI window, not in `supervisor.json`.

Providers behave differently. Codex draws on a shared, load-balanced pool: downgrading its
model does not free capacity, so keep one validated model (or leave it unset for the
provider default), cap and stagger concurrent high/xhigh Codex turns, and vary effort
rather than model. Claude is weekly-budget bound: make `sonnet` the workhorse and reserve
`opus` plus the highest efforts for short, high-risk passes. `fable` is an
experimental/specialist model, not a routine default. Use discrete effort tokens
(`low`/`medium`/`high`/`xhigh`/`max`, per each CLI's own set) — a hyphenated range like
`medium-high` is not a valid value.

The agents' own model/effort, context-reset, and fresh-reviewer discipline is documented
for them in `docs/AGENT-MANUAL.md` §1.

**Verifying the flags reached the CLI.** A few things trip up a check done right after a
relaunch:

- When the value lives **only** in `supervisor.json`, a wrapped agent is idle until first
  dispatched, so no CLI child exists and the model flag appears nowhere in the process tree
  until it handles a message. (A `wrap --model`/explicit-tail model, by contrast, is already
  on the wrapper argv.) Send it work, then inspect **while the turn is active** — a
  short-lived child can exit before you look.
- Inspect the **child** `claude.exe` / `codex.exe`, not the wrap: the wrapper's own command
  line contains `python -m agenttalk`, which false-matches a naive `-m`/`--model` search.
  The child spelling differs by provider — Claude passes `--model <m> --effort <e>`, Codex
  passes `-m <m> -c model_reasoning_effort=<e>`. e.g.
  `Get-CimInstance Win32_Process -Filter "ParentProcessId=<wrapPid>" | Select-Object ProcessId,Name,CommandLine`.
  This is a best-effort diagnostic — CIM command-line access can be denied by OS policy, so
  don't treat it as verification authority. Supervisor model/effort does not configure an
  interactive liaison window (that is set in the window's own CLI), so an unset interactive
  agent is expected, not a fault.

Request a restart:

```powershell
agenttalk request-restart --for codex-dev --from claude-lead --reason reload-after-config
```

Protected agents are the operator-facing liaison and active lead-role agents.
The supervisor will not auto-kill them. A manual restart of a protected agent
requires `--force-protected`; if the protected agent still has a fresh
heartbeat, the operator-facing requester must also pass
`--acknowledge-live-protected-kill`.

Kill switch:

```powershell
New-Item .agenttalk\supervisor.kill -ItemType File
```

While the kill switch exists, read-only commands still work, but mutating
supervisor automation is disabled. Remove the file to re-enable automation.

## 9. Work lanes and isolated worktrees

A lane is a scoped assignment tied to a domain registry entry. It records the
assignee, base SHA, target ref, and path prefixes. The default `lane assign`
provisions an isolated worktree so concurrent builders do not collide.

Before assigning a docs lane, author a minimal domain registry. In PowerShell,
this writes `.agenttalk/domains.json` as UTF-8 without a BOM for the roster
names from Section 4:

```powershell
$domainsJson = @'
{
  "schema_version": 1,
  "domains": {
    "docs": {
      "title": "Documentation",
      "owners": { "groups": ["devs"] },
      "reviewers": { "groups": ["reviewers"] },
      "curators": { "agents": ["claude-lead"] },
      "owned_globs": ["docs/**", "README.md"]
    }
  },
  "shared_paths": [
    {
      "glob": "pyproject.toml",
      "category": "package-metadata",
      "requires": "shared-lease-or-lead-approval",
      "default_reviewers": { "groups": ["reviewers"] }
    }
  ]
}
'@
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path (Resolve-Path .agenttalk) 'domains.json'), $domainsJson, $utf8NoBom)

agenttalk domain validate
agenttalk domain show docs
agenttalk domain check-path docs/USER-MANUAL.md README.md pyproject.toml
```

Assign a lane:

```powershell
agenttalk lane assign --id docs-manual --from claude-lead --assignee codex-dev --domain docs --base main --target main --path docs
```

Find the worktree:

```powershell
agenttalk lane workspace --id docs-manual
```

Check before delivery:

```powershell
agenttalk lane check --id docs-manual
agenttalk lane check --id docs-manual --json
```

Deliver:

```powershell
agenttalk lane deliver --id docs-manual --from codex-dev
```

Delivery is two-phase and retryable:

```text
active -> prepared (not consumable)
       -> publish_pending (lane generation + instance checkpoint)
       -> committed (consumable delivery evidence)
       -> cleanup_pending|cleanup_failed -> cleanup complete
```

The terminal diff, target, gates, registry, epoch, and worktree provenance are
rebound before publication. If preparation, the first state save, publication,
or cleanup fails, run the same `lane deliver` again. It resumes the bound
transaction; it does not expose an uncommitted GO or mint a second delivery.
A committed artifact stays valid while cleanup is pending. Force, abandon, and
reassign refuse to bypass `publish_pending`.

Shared path approval:

```powershell
agenttalk lane approve-shared --id docs-manual --path pyproject.toml --from claude-lead --reason package-metadata-needed
```

Lanes are advisory deliver gates, not file locks. A GO means the current
evidence says the lane is in bounds and mergeable now. It does not prevent
someone from editing files in Git or the OS.

Release-class lanes require their provisioned worktree. `--no-worktree` is
limited to a lane declared `--advisory` and still requires an explicit
`--worktree-waiver-reason`. The recorded authority is advisory audit context:
it does not trust mutable role labels, does not claim isolation happened, and
cannot satisfy a release close.

Common HOLD causes include stale epoch, stale domain registry, out-of-bounds
paths, unowned paths, active lane overlap, shared path missing approval, merge
conflict, degraded merge check, or gate hold.

### Onboarding before implementation

For a new project or a large existing project, start by recording what the team
learned before assigning implementation. This is a ledger, not an analyzer: it
captures evidence, drift, and open questions so the lead can see whether the
team is ready to create work.

Create a run:

```powershell
agenttalk onboarding create --id ob-api --from claude-lead --title "API onboarding" --base-ref main
```

Record evidence as the team reads code and docs:

```powershell
agenttalk onboarding record --id ob-api --from codex-dev --kind segment --key cli --status accepted --summary "CLI parser and README command reference mapped." --path src/agenttalk/cli.py --path README.md
agenttalk onboarding record --id ob-api --from codex-review --kind drift --key docs.cli.reference --status open --segment cli --source docs --confidence medium --summary "README command table may lag parser help."
agenttalk onboarding record --id ob-api --from codex-test --kind unknown --key release.owner --status open --blocking --summary "Need operator confirmation of the release owner."
```

Inspect or update the run:

```powershell
agenttalk onboarding show --id ob-api
agenttalk onboarding state --id ob-api --from claude-lead --state ready-for-work --summary "Required segments accepted; no blocking unknowns remain."
```

Use **Onboarding** in the dashboard for the same selected-root view. Treat its
claims as untrusted project evidence until the lead, reviewers, tests, and
gates turn them into work items, knowledge notes, or release evidence.

## 10. Assurance, gates, reviews, and release use

Assurance is an opt-in HOLD/GO layer. It records evidence and makes unsafe
closure harder, but the operator still owns the decision.

Gate examples:

```powershell
agenttalk gate set --from claude-lead --scope release:manual --name tests --status unknown --severity blocker
agenttalk gate list
agenttalk gate check --scope release:manual
agenttalk gate waive --from claude-lead --scope release:manual --name tests --operator claude-lead --reason ci-unavailable --expires 2026-07-14
```

A blocker gate in `red` or `unknown` yields HOLD unless a valid scoped waiver
exists. A blocker gate can be set green only with automation evidence. Use a
waiver only when the operator deliberately accepts the residual risk; record the
reason and expiration.

Before irreversible actions tied to a request:

```powershell
agenttalk check --for claude-lead --to-request <request-id> --gates
```

Close records aggregate gates and review evidence for a frozen revision:

```powershell
agenttalk close open --id rel-070 --from claude-lead --scope release --revision <sha> --non-lane-isolation-not-asserted
agenttalk close check --id rel-070
agenttalk close publish --id rel-070 --from claude-lead --verdict go
```

Every mutation of an existing close is serialized and compares the loaded
`generation` plus immutable `instance_id`. A conflict means another writer or
replacement won; the command fails closed, and the safe action is to reload and
retry. Creation is exclusive, force-open creates a new instance under lock, and
a legacy close is upgraded inside that lock rather than overwritten unchecked.
Changing the revision on reopen clears the old dirty-worktree artifact.

For a release barrier, use the idempotent form:

```powershell
agenttalk close publish --id rel-070 --from claude-lead --verdict go --bump-barrier
```

The persisted GO binds the barrier to close id, instance, revision, and
generation. If the barrier send or epoch stamp fails, rerun the exact command.
It resumes the one validated binding and sends at most once; a duplicate or
mismatch HOLDs. Do not send a separate fresh barrier to work around a partial
publish.

In `signoffs.json`, boolean fields such as `use_default_reviewers`,
`include_domain_reviewers`, `allow_na`, and `override_counts` must be literal
JSON booleans. Counter ids must be unique across the whole close, including
different lenses. Core risk classes are `none`,
`unknown`, `release`, `device`, `accessibility`, `security`, `performance`,
`persistence`, `docs-contract`, and `quality`; projects may use a validated
namespaced extension such as `project:mobile`.

Review results should separate inspected evidence from executed evidence.
`tests_referenced` means read or considered. `tests_executed` means actually
run with an observed result. Do not treat a confident approval without executed
evidence as a release gate.

For release protocol and attestation, read [ASSURANCE.md](ASSURANCE.md) and
[DESIGN.md](DESIGN.md) instead of copying their maintainer detail here.

## 11. Knowledge, domains, and lessons

Domains describe ownership and expertise for repo areas. The examples below
assume the `docs` domain from Section 9 exists. Inspect it:

```powershell
agenttalk domain list
agenttalk domain show docs
agenttalk domain check-path docs/USER-MANUAL.md README.md
agenttalk domain validate
```

Knowledge notes are durable project memory. Pointer notes capture the small
insight, not a copy of the artifact; their anchor points to the code, request,
SHA, symbol, or work package. Lessons capture repeatable process learning in the
same ledger. Bodies are untrusted advisory data, never authority or instructions:
reverify the evidence or anchor before acting.

Publish an uncurated note:

```powershell
agenttalk knowledge publish --from codex-dev --domain docs --type gotcha --key docs-help-flags --anchor-kind path --path docs/USER-MANUAL.md -m "Verify every documented flag against agenttalk --help."
```

Curate it:

```powershell
agenttalk knowledge curate verify --from claude-lead --domain docs --key docs-help-flags
```

Pull the default mixed view, or request an explicit kind:

```powershell
agenttalk knowledge pull --domain docs
agenttalk knowledge pull --type lesson --scope docs
agenttalk knowledge pull --type lesson --include-uncurated
agenttalk knowledge search explicit-root --scope docs
agenttalk knowledge onboard --scope docs --lesson-limit 5
```

Without `--type`, pull/search/onboard include pointer notes and lessons. `--scope`
or `--tags` implies lesson-only retrieval; combining either with a non-lesson
`--type` is a usage error. Onboard includes lessons by default; use
`--exclude-lessons` to omit them (`--include-lessons` is a deprecated no-op).
Mixed `--json` returns `knowledge-view-v1` with separate `notes`, `lessons`,
pre-limit `totals`, `truncation`, and `problems`. Explicit `--type ... --json`
keeps its prior shape; `--output-schema legacy --json` emits the old pointer-only
array for compatibility.

Lessons are a note type. They capture repeatable process learning, can have
review and expiry dates, and should eventually be promoted into skills, tests,
gates, or docs when they become permanent practice.

Lessons default to the virtual `process` domain, curated by the current
operator-facing liaison or a lead. That virtual domain is lesson-only. A real
registered `process` domain overrides it and supplies normal owner/curator
authority; non-lesson process notes always require that real registry entry.

Publish a lesson with its required review fields:

```powershell
agenttalk knowledge publish --from claude-lead --type lesson --key docs.explicit-root --scope docs --trigger manual-docqa --evidence-ref q-docqa-001 --applies-to docs --review-after 2026-08-01 --expires-at 2027-01-01 --anchor-kind request --request-id q-docqa-001 -m "Use explicit roots when documenting reusable commands."
agenttalk knowledge pull --type lesson --scope docs --tags docs --include-uncurated
```

Knowledge freshness is scoped to the effective domain definition. Editing an
unrelated domain adds a caution but does not hide the note; editing its own domain
requires re-verification. Historical rows without the scoped hash remain visible
with a legacy-freshness caution. Verify and retract re-stamp registry hashes while
preserving the original Git/anchor baseline.
The curation registry recheck coordinates supported writers that honor the shared
lock. A manual `domains.json` edit can bypass that lock, but the stored domain hash
makes the resulting event stale rather than silently authoritative.

`agenttalk sync --for <agent>` includes a capped Lessons to check section when
accepted, not-expired lessons match the current work context or supplied
`--lesson-tag` values. Lessons are advisory memory, not authorization.

For `agenttalk wrap --loop`, lesson surfacing is automatic. The wrapper matches
accepted lessons against the inbound message, adds a Lessons to check section to
the child prompt, and appends a pointer-only exposure event to
`.agenttalk/knowledge/lesson-exposures.jsonl` after the prompt is handed to the
child process. The exposure event is audit telemetry: it records which accepted
lesson was surfaced to which agent/message, not whether the model read or
applied it.

To inspect the same audit trail visually, open the dashboard and choose
**Learning**. For scripting, `GET /api/learning` returns the selected root's
accepted active lessons by default, with pointer-only exposure telemetry.
Use `status=proposed`, `status=stale`, `status=retired`, or `status=all` only
when you intentionally want diagnostic rows outside the accepted active set.

## 12. Troubleshooting

Run doctor first:

```powershell
agenttalk doctor
agenttalk doctor --json
```

Common cases:

- Heartbeat-stale lead. The lead-chat panel says unavailable when the
  operator-facing lead has no fresh heartbeat. Start `wait`, run the lead under
  `wrap --loop`, or install the interactive Claude heartbeat and checkpoint
  hooks with
  `agenttalk supervise --install-activity-hook --interactive-for <lead>`.
- No operator-facing lead. Set one with
  `agenttalk roster set-operator-facing <agent>`, or set exactly one active
  `role=lead` as the fallback.
- Stacked waiters. If `wait --refuse-stacked-wait` exits 6 or doctor reports a
  duplicate waiter, close the duplicate terminal. One live consumer per mailbox
  is supported.
- Coordination stall. `attention`, `doctor`, `status`, and the dashboard show
  the same advisory when an explicit waiter targets a supervisor-confirmed
  unavailable agent, or when a requested restart remains behind a launch
  barrier. Reassign the request or restore the named agent. These warnings never
  kill, restart, release, reroute, or change a gate.
- Stale open threads. Run `agenttalk sync --for <agent>` and
  `agenttalk threads --for <agent> --all`. Use `reply`, `rescind`, or
  `ack --to-request` only when the thread is actually handled.
- Dead letter. A wrapped agent hit its retry ceiling for a valid message.
  Inspect with `agenttalk dead-letter list` and
  `agenttalk dead-letter show --agent <agent> --id <id>`. Requeue only when the cause is fixed, or
  mark handled with `dead-letter resolve`. Resolve also closes matching wrapper
  dead-letter notice escalations so they do not remain as phantom current work.
  Use `agenttalk dead-letter purge --resolved --from <liaison>` to archive old
  resolved payloads out of the live sink; archived rows are no longer requeueable
  by the live `dead-letter requeue` command unless restored.
- Wrapper crash. Check `supervise --report`, `supervise --plan`, wrapper
  health files, and the dead-letter sink. The wrapper owns the heartbeat for
  wrapped agents.
- Dashboard unavailable. Use `agenttalk dashboard --port 0` to avoid port
  conflicts. `dashboard` is loopback-only and has no `--host` flag.
- Permissions or path issues. Use `agenttalk whoami --for <agent>`,
  `agenttalk status`, and `agenttalk doctor`. Put `--root` before the
  subcommand. Check that Codex has the project sandbox block if it must call
  agenttalk from inside its sandbox.
- Corrupt or invalid messages. Run `agenttalk prune --invalid --dry-run`, then
  `agenttalk prune --invalid` if the selection is correct.
- Supervisor disabled. Remove `.agenttalk/supervisor.kill` after you are ready
  for automation to mutate state again.

## 13. Command reference cheat sheet

This is a compact operator map, not a full argparse dump. Use
`agenttalk <command> --help` for exact flags.

| Workflow | Commands | Use |
| --- | --- | --- |
| Setup | `init`, `install-skills`, `codex-config`, `doctor`, `whoami` | Create a store, install skills, enable Codex sandbox calls, verify health and identity. |
| Identity | `roster`, `roster add`, `roster set-role`, `roster set-group`, `roster set-operator-facing`, `roster retire`, `roster rename` | Manage names, roles, groups, and the human liaison. |
| Messaging | `send`, `reply`, `broadcast`, `propose`, `composing`, `rescind` | Start, answer, fan out, negotiate, show in-flight drafting, or supersede tracked work. |
| Reading | `recv`, `drain`, `wait`, `sync`, `threads`, `status`, `tail` | Read inboxes, block for new work, rejoin after restart, inspect obligations, or passively monitor. |
| Operator routing | `escalate`, `attention`, `relay` | Route human decisions, inspect the attention queue, and carry operator answers or commands through the bus. |
| Safety checks | `check`, `barrier`, `gate`, `close` | Check request currentness, mark epochs, manage gates, and aggregate HOLD/GO release evidence. |
| Lanes | `domain`, `lane assign`, `lane workspace`, `lane check`, `lane deliver`, `lane approve-shared` | Bound work to domain paths and deliver from isolated worktrees. |
| Onboarding | `onboarding create`, `onboarding record`, `onboarding show`, `onboarding state`, `onboarding list` | Track codebase-analysis segments, claims, drift, and blocking unknowns before implementation. |
| Knowledge | `knowledge publish`, `knowledge curate`, `knowledge pull`, `knowledge search`, `knowledge onboard` | Capture, verify, retrieve, and search durable project notes and lessons. |
| Supervision | `supervise --bootstrap-check`, `supervise`, `wrap`, `heartbeat`, `request-restart`, `request-launch`, `managed-lead-loop`, `deadman` | Verify the roster is a live team, run unattended agents, maintain liveness, request restarts, and monitor stale work. |
| Recovery | `checkpoint save`, `checkpoint resume`, `checkpoint show`, `dead-letter list`, `dead-letter show`, `dead-letter requeue`, `dead-letter resolve`, `dead-letter purge --resolved`, `prune`, `compact`, `reset` | Preserve compact-resume anchors, inspect poison messages, archive resolved poison evidence, quarantine invalid files, archive old messages, or clear active state. |
| Web | `dashboard`, `serve`, `start` | Open local loopback UI surfaces and optional browser intent enqueueing. |

## 14. Glossary

Lead
: A coordinating agent or role. A lead decomposes and routes work, but lead
  prose does not move protocol state by itself.

Liaison
: The `operator_facing` agent. It is the human operator's primary contact and
  the default escalation target.

Gate
: A durable scoped status such as green, red, or unknown. Blocker gates feed
  HOLD/GO checks.

Close
: An auditable release or milestone verdict over a frozen revision, gates, and
  review evidence.

Lane
: A scoped work assignment with path bounds, base and target refs, and an
  optional managed worktree.

Wrapper
: `agenttalk wrap`, which runs a real CLI through a progress adapter, owns the
  idle bus wait, stamps heartbeats, and preserves session continuity.

Supervisor
: The generated external monitor that launches, watches, and relaunches agents
  from a read-only liveness report and action plan.

Knowledge note
: A durable project-memory record with a small insight and an anchor to the
  artifact that proves or contextualizes it.

Lesson
: A knowledge note type for repeatable process learning, with curation,
  review, expiry, and sync visibility.

Dead letter
: A valid message moved out of a wrapped agent's inbox after deterministic
  failures so the mailbox is not blocked forever.

Intent
: A typed local dashboard action record that the server validates and the
  supervisor may drain. It is not arbitrary browser authority.

Escalation
: A tracked operator-input request routed to the liaison or sole lead by
  `agenttalk escalate`.
