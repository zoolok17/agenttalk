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
python -m pip install git+https://github.com/zoolok17/agenttalk.git@v0.71.0
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
agenttalk reply --from codex-rev --to-request <request-id> --kind review-result -m "REQUEST-CHANGES: missing test."
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
```

Use `wait` for live listening:

```powershell
agenttalk wait --for codex-rev --timeout 1800
agenttalk wait --for codex-rev --to-request <request-id> --kind review-result
```

If `wait --refuse-stacked-wait` exits 6, another live waiter already owns that
mailbox. Stop the duplicate; one live consumer per agent is the supported rule.

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

Read what the supervisor sees:

```powershell
agenttalk supervise --report
agenttalk supervise --plan
```

Run the monitor in a dedicated terminal on Windows:

```powershell
.\.agenttalk\supervisor.ps1
```

For durable hosting, use the generated Scheduled Task helper:

```powershell
.\.agenttalk\supervisor-task.ps1 -Action install
.\.agenttalk\supervisor-task.ps1 -Action status
```

Heartbeat freshness is the liveness authority. A fresh heartbeat means healthy.
A stale heartbeat can recover only when the agent is instrumented by the
activity hook or by `agenttalk wrap --loop`. Otherwise the supervisor warns and
does not kill the agent.

Manual windows plus hook:

```powershell
agenttalk supervise --install-activity-hook
agenttalk supervise --install-activity-hook --codex
agenttalk supervise --install-activity-hook --interactive-for claude-lead
```

Use the neutral hook for supervised/manual agents launched with
`AGENTTALK_SELF`. Use `--interactive-for <lead>` only for the current
operator-facing human Claude liaison window. It writes a Claude-only fallback
hook. Non-liaison windows should set `AGENTTALK_SELF` instead.

Recommended unattended path:

```powershell
agenttalk wrap --for codex-dev --cli codex --loop -- codex -a never -s workspace-write -C D:\Projects\example
```

In supervisor config, wrapped agents use Python as `windows_file`; the real CLI
goes after `--` in `windows_args`. See
[supervisor-tutorial.md](supervisor-tutorial.md) for complete config examples.

Request a restart:

```powershell
agenttalk request-restart --for codex-dev --from claude-lead --reason reload-after-config
```

Protected agents are the operator-facing liaison and active lead-role agents.
The supervisor will not auto-kill them. A manual restart of a protected agent
requires `--force-protected`.

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

Shared path approval:

```powershell
agenttalk lane approve-shared --id docs-manual --path pyproject.toml --from claude-lead --reason package-metadata-needed
```

Lanes are advisory deliver gates, not file locks. A GO means the current
evidence says the lane is in bounds and mergeable now. It does not prevent
someone from editing files in Git or the OS.

Use `--no-worktree` only with an explicit `--worktree-waiver-reason`. A waiver
means the operator accepts reduced isolation for that lane; it is evidence, not
a claim that isolation happened.

Common HOLD causes include stale epoch, stale domain registry, out-of-bounds
paths, unowned paths, active lane overlap, shared path missing approval, merge
conflict, degraded merge check, or gate hold.

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

Knowledge notes are durable, pointer-shaped project memory. They capture the
small insight, not a copy of the artifact. The anchor points to the code,
request, SHA, symbol, or work package.

Publish an uncurated note:

```powershell
agenttalk knowledge publish --from codex-dev --domain docs --type gotcha --key docs-help-flags --anchor-kind path --path docs/USER-MANUAL.md -m "Verify every documented flag against agenttalk --help."
```

Curate it:

```powershell
agenttalk knowledge curate verify --from claude-lead --domain docs --key docs-help-flags
```

Pull active notes:

```powershell
agenttalk knowledge pull --domain docs
agenttalk knowledge pull --type lesson --scope docs
agenttalk knowledge pull --type lesson --include-uncurated
```

Lessons are a note type. They capture repeatable process learning, can have
review and expiry dates, and should eventually be promoted into skills, tests,
gates, or docs when they become permanent practice.

Publish a lesson with its required review fields:

```powershell
agenttalk knowledge publish --from claude-lead --type lesson --key docs.explicit-root --scope docs --trigger manual-docqa --evidence-ref q-docqa-001 --applies-to docs --review-after 2026-08-01 --expires-at 2027-01-01 --anchor-kind request --request-id q-docqa-001 -m "Use explicit roots when documenting reusable commands."
agenttalk knowledge pull --type lesson --scope docs --tags docs --include-uncurated
```

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
  `wrap --loop`, or install the interactive hook with
  `agenttalk supervise --install-activity-hook --interactive-for <lead>`.
- No operator-facing lead. Set one with
  `agenttalk roster set-operator-facing <agent>`, or set exactly one active
  `role=lead` as the fallback.
- Stacked waiters. If `wait --refuse-stacked-wait` exits 6 or doctor reports a
  duplicate waiter, close the duplicate terminal. One live consumer per mailbox
  is supported.
- Stale open threads. Run `agenttalk sync --for <agent>` and
  `agenttalk threads --for <agent> --all`. Use `reply`, `rescind`, or
  `ack --to-request` only when the thread is actually handled.
- Dead letter. A wrapped agent hit its retry ceiling for a valid message.
  Inspect with `agenttalk dead-letter list` and
  `agenttalk dead-letter show --agent <agent> --id <id>`. Requeue only when the cause is fixed, or
  mark handled with `dead-letter resolve`.
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
| Knowledge | `knowledge publish`, `knowledge curate`, `knowledge pull`, `knowledge search`, `knowledge onboard` | Capture, verify, retrieve, and search durable project notes and lessons. |
| Supervision | `supervise`, `wrap`, `heartbeat`, `request-restart`, `request-launch`, `managed-lead-loop`, `deadman` | Run unattended agents, maintain liveness, request restarts, and monitor stale work. |
| Recovery | `dead-letter list`, `dead-letter show`, `dead-letter requeue`, `dead-letter resolve`, `prune`, `compact`, `reset` | Inspect poison messages, quarantine invalid files, archive old messages, or clear active state. |
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
