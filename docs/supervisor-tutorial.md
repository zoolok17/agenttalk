# Tutorial: the 24/7 supervisor and the progress wrapper

This is a hands-on walkthrough of running agenttalk agents **unattended**
— a background monitor that keeps named agents alive across provider
outages, rate-limit windows, and stuck turns, and restarts them
**with their session context intact** so they pick up where they left
off.

It is optional. The default agenttalk workflow is interactive: you open
a terminal per agent and run `/agenttalk.listen`. The supervisor is for
the case where you want agents to survive being left alone for hours.

By the end you will have:

- a background monitor watching one or more agents,
- agents that auto-restart on crash/outage and resume their session,
- wrapped agents for the recommended hands-off durable-listening path,
  with manual-listen agents still available for interactive or legacy use.

> **Platform.** The generated monitor is **PowerShell** (`supervisor.ps1`)
> on Windows. A POSIX monitor is a follow-up; the CLI surface
> (`supervise`, `wrap`, `request-restart`, `heartbeat`) is
> cross-platform, only the generated monitor script is Windows-first.

---

## 1. The mental model

Three ideas carry the whole feature:

1. **Health authority depends on the launch mode.** A manual listener
   stamps a small `heartbeat` file as it works and while it idles in
   `wait`; heartbeat freshness remains its stuck signal. A wrapped
   listener additionally publishes one strict turn-lifecycle record.
   Only a validated `idle` phase can be `HEALTHY_IDLE`. During an active
   turn the supervisor independently discovers the real CLI brain and
   requires real adapter progress. Missing, malformed, or ambiguous
   evidence is non-green and never automatic kill authority. Before that
   child-health decision, an invalid/truncated owned tree creates a
   `PROCESS_TREE_INVALID`/`PROCESS_TREE_TRUNCATED` HOLD and preserves any
   restart request.

2. **The supervisor is an external monitor, not a daemon inside the
   bus.** `agenttalk supervise` only computes a read-only **report**
   (who is alive/stale) and an **action plan** (the decision table).
   A generated PowerShell script polls those and does the actual
   launching/killing. The bus stays just files.

3. **Every restart resumes the agent's session, so it keeps its
   context** — the branch it was on, the files it inspected, the turn it
   was mid-way through. *How* it resumes depends on the agent:
   - **manual Claude** — the supervisor pins the minted session id and
     relaunches with `--resume <id>`;
   - **manual Codex** — the supervisor relaunches with `resume --last`,
     picking up the most recent workspace session in that agent's
     (seeded/isolated) `CODEX_HOME` once it is ready (so keep one Codex
     per home, or the "last" session is ambiguous);
   - **wrapped agents (Claude or Codex)** — the wrapper itself persists
     the Claude session id / Codex `thread_id` and reload-resumes it,
     across both its own turns and a supervisor relaunch.

   Restart-with-context is the headline — the difference between "the
   agent is back" and "the agent is back and knows what it was doing."

There are **two ways** an agent can be made stuck-recoverable:

- **Manual-listen agent + activity hook.** A normal `/agenttalk.listen`
  agent with the `agenttalk heartbeat` PostToolUse hook installed. It
  heartbeats at every tool boundary plus the idle wait loop.
- **Wrapped agent.** The agent runs *through* `agenttalk wrap --loop`,
  which owns the idle wait and heartbeat, writes the strict runtime
  phase/progress record, and streams the agent's progress to the
  console. No hook is needed.

Until an agent can confirm "stuck" (hook installed **or** wrapped), a
stale heartbeat is **warn-only** — never a kill. An un-instrumented
agent is never mistaken for stuck.

---

## 2. Prerequisites

- agenttalk installed and a store initialized in your project
  (`agenttalk init --here --agents ...`). See the README quickstart.
- PowerShell Core 7+ on Windows. Stable 7.4+ is recommended. Stable
  7.0-7.3 and prereleases run with warnings; Windows PowerShell 5.1 is
  unsupported and refused.
- The **real** CLI executables (not shims):
  - Claude Code: e.g. `C:\Users\you\.local\bin\claude.exe`
  - Codex: the native exe, e.g.
    `...\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`
    (a `.cmd`/npm/PowerShell shim hands off and exits — the supervisor
    needs the real exe).
- For wrapped agents: the **Python** executable that runs agenttalk
  (e.g. `...\Python314\python.exe`).

> **Unattended means no human to approve prompts.** The supervisor seeds
> each agent into a never-prompt, never-elevate, workspace-write mode
> (Claude `--permission-mode bypassPermissions` + a seeded
> `.claude/settings.json`; Codex a seeded isolated `CODEX_HOME`
> `config.toml` with `approval_policy=never`, `sandbox_mode=workspace-write`).
> Only run this on a repo you trust the agents to modify autonomously.

---

## 3. Scaffold the config

From your project root:

```powershell
agenttalk supervise --init
```

This creates the operator config plus four generated artifacts under
`.agenttalk/`. `--init --force` refreshes only generated files; it preserves an
existing `supervisor.json` byte-for-byte and does not touch runtime state.

- **`supervisor.json`** — the config you fill in: cadence knobs plus a
  per-agent block describing how to launch each agent.
- **`supervisor.ps1`** — the generated PowerShell monitor. You run this;
  you do not edit it. Its live loop uses an internal executable poll whose caller
  must be an OS-verified child of the live selected PowerShell host (launch /
  relaunch-with-resume / scoped kill / warn). `-DryRun` uses the non-advancing
  `supervise --plan` observation instead.
- **`bin/agenttalk.cmd`** — a tiny shim the monitor calls so its own bus
  commands resolve to the right Python/agenttalk regardless of your PATH.
  You don't invoke it directly.
- **`supervisor-task.ps1`** — the generated Scheduled Task helper.
- **`deadman.ps1`** — the generated content-blind mail-age check wrapper.

The four generated files carry one deterministic schema/generation marker and
are validated as a set. If an upgrade or interrupted refresh leaves stale or
mixed files, regenerate them without changing the operator config:

```powershell
agenttalk supervise --refresh-scripts
```

Once you start the monitor it also writes **`supervisor-state.json`** —
script-owned bookkeeping (per-agent launcher pids, pinned Claude session
ids, backoff timers). That is the monitor's own state, **not** bus state,
and its validated previous generation lives in `supervisor-state.json.bak`.
Readers prefer a valid primary and can fall back to the backup without
rewriting a corrupt primary. If both copies are invalid, planning and action
fail closed. Do not delete these files casually: doing so while the monitor is
stopped discards launch/session/backoff continuity even though bus messages and
cursors remain.

Each supervisor-launched `agenttalk wrap` process also gets distinct stdout and
stderr logs outside the checkout. Legacy/manual direct CLI launches are not
redirected because an arbitrary executable cannot enforce the cooperative byte
bound described below. On Windows wrapper logs live under
`%LOCALAPPDATA%\agenttalk\wrapper-logs\<project-hash>\agent-<agent-hash>\<generation>\`;
on POSIX the base is an absolute `$XDG_STATE_HOME` or `~/.local/state`. Relative
ambient state paths are ignored. The path-derived project hash keeps projects
separate without relying on adopter repositories to ignore `.agenttalk/`;
`agenttalk init` does not provision that ignore rule. Treat these files as
sensitive: they can contain model output, tool output, and tracebacks. The
agent directory is the first 16 hex characters of SHA-256 over the exact UTF-8
agent name, prefixed with `agent-`; hashing avoids Windows reserved-name and
trailing-dot aliases.

Before starting the process, the supervisor creates a new immutable generation
across both the persistent and temporary fallback roots and prunes older
generations back toward a quota of four (`WRAPPER_LOG_GENERATIONS`). Once
`agenttalk wrap` begins command dispatch, Python-level writes through its
standard streams are bounded to 1 MiB per stream using four fixed segments;
suffixes `.1` through `.3` retain the newest output after the initial
redirect segment fills. Interpreter/package bootstrap output written before
that entry point, and direct native/file-descriptor writes, are not
intercepted by the cooperative Python stream bound. The switch to a new
immutable destination happens before the relaunch, so the wrapper that just
died is never overwritten.

A generation is retained forever, uncounted against the quota, until the
wrapper process itself confirms it - from inside `agenttalk wrap`, right
after authenticating and installing both bounded stream tees, which is the
earliest point that proves this launch actually reached command dispatch.
The supervisor never commits a generation on the launcher's behalf (a
process existing, or even returning a PID, is not proof the wrapper reached
that point), so a wrapper that dies before confirming leaves its generation
preserved and unpruned rather than evicting real evidence for a launch
attempt whose outcome was never known. Because pruning runs when a new
generation is created - before that new generation's own fate is decided -
a string of launches that all successfully confirm settles at one MORE than
the quota (five, by default) rather than exactly the quota: pruning trims
existing CONFIRMED generations down to four before creating the next one,
which itself becomes a fifth confirmed generation once it succeeds, and
nothing prunes again until the next launch repeats the same pattern. This
is a known, currently-accepted bound violation, not a rare edge case -
proven by running repeated successful launches against the code as shipped
and counting the generations left on disk - to be revisited alongside the
broader retention-timing question in a follow-up round. If the persistent
root is unavailable, the supervisor warns and tries the OS temporary
directory. If a stale handle or filesystem error prevents cleanup, the
recovery launch continues with a unique generation and warns; the quota can
remain exceeded (independent of the off-by-one above) until that filesystem
problem is resolved. If neither root can accept a new generation, recovery
launches without redirection.

Which of the three tail segments (`.1`-`.3`) is currently being written to is
itself recorded in a sibling `<segment-base>.cursor` file, updated every time
a segment is opened - not inferred from filesystem modification times, which
are ambiguous on coarse-resolution filesystems and wrong across a backward
clock adjustment. A missing cursor - true of every generation already on
disk the moment this recording was added - falls back to segment `.1`, but
always in append mode, never by truncating it: the worst case on an upgrade
boundary is old, unrelated content from an earlier rotation followed by the
new instance's content in one file, never destroyed content. This preserves
the property the quota+1 paragraph above does not: this ring never trades
away crash evidence for a tidier bound.

On Windows the generated supervisor gives the child an explicit allowlist of
only stdin (`NUL`) and the two log handles. This avoids leaking the
supervisor's caller pipes or state-file locks into a long-lived wrapper. If the
safe logging launcher cannot initialize, recovery still launches without the
PowerShell redirect and emits a warning; logging never grants launch authority
or blocks recovery.

Wrapper stderr also includes compact JSON lines for facts such as
`turn_started`, `child_spawned`, `child_exited`, `turn_ended`, and wrapper
termination. They are post-mortem evidence only. They are not heartbeat,
progress, health, or restart inputs, and they never describe the wrapper as
healthy or alive. An OOM, hard kill, or power loss cannot write a final event;
the gap after the last factual line is the evidence in that case.

Heartbeat freshness is also future-bounded. A timestamp farther ahead than the
configured clock-skew allowance cannot authorize a healthy state; a timestamp
within the allowance can.

The scaffold ships **two example agent blocks** so you can copy whichever
archetype you need:

- `AGENT_NAME` — a **manual-listen** agent for interactive or legacy use.
- `AGENT_NAME_WRAPPED` — a **wrapped** agent driven through `wrap --loop`,
  the recommended default for hands-off durable listening.

---

## 4. Fill in `supervisor.json`

### Top-level knobs (sane defaults shown)

```jsonc
{
  "schema_version": 2,
  "poll_seconds": 15,              // how often the monitor checks
  "stuck_after_seconds": 120,      // global stale threshold (per-agent overrides win)
  "launch_grace_seconds": 120,     // startup grace before liveness is judged
  "claude_permission_mode": "bypassPermissions",
  "backoff": { "base_seconds": 30, "cap_seconds": 900, "reset_after_seconds": 180 },
  "agents": { /* one block per agent */ }
}
```

`backoff` throttles relaunch storms: a flapping agent waits
`base..cap` seconds (exponential) between attempts, resetting after
`reset_after_seconds` of health.

### A manual-listen agent

```jsonc
"claude-dev": {
  "cli": "claude",
  "auto_restart": true,
  "activity_hook": false,          // flip to true AFTER installing the hook (step 5)
  "cwd": "D:\\Projects\\example",
  "env": { "AGENTTALK_SELF": "claude-dev" },
  "launch": {
    "windows_file": "C:\\Users\\you\\.local\\bin\\claude.exe",
    "windows_args": ["{SESSION_ARGS}"]
  }
}
```

The literal `{SESSION_ARGS}` element is spliced by the executor with the
session tokens — fresh on first launch, `--resume <id>` on relaunch. The
launch prompt drops the agent straight into `/agenttalk.listen`.

> **Best-effort / legacy for unattended use.** A manual-listen agent is
> fine interactively and on a fresh launch, but a **resumed** session may
> not reliably re-enter the listen loop — so it never heartbeats, never
> reaches readiness, and the supervisor relaunches it. After
> `max_readiness_retries` (default 3) such never-ready relaunches the
> supervisor **stops** and surfaces `READINESS_GAVE_UP` (it will not churn
> forever; clear it with a fresh heartbeat or a restart-request). For
> hands-off supervision prefer a **wrapped** agent (below): the wrapper
> owns the heartbeat regardless of whether the model re-enters a prompt.

### A wrapped agent (RECOMMENDED default for hands-off codex/claude)

```jsonc
"codex-dev": {
  "cli": "codex",
  "auto_restart": true,
  "wrapped": true,
  "stuck_after_seconds": 1200,     // see the threshold note below
  "cwd": "D:\\Projects\\example",
  "env": { "AGENTTALK_SELF": "codex-dev" },
  "launch": {
    "windows_file": "C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python314\\python.exe",
    "windows_args": [
      "-m", "agenttalk", "--root", "{ROOT}", "wrap", "--for", "codex-dev",
      "--cli", "codex", "--loop", "--",
      "C:\\path\\to\\codex.exe", "-a", "never", "-s", "workspace-write",
      "-C", "D:\\Projects\\example", "--disable", "hooks"
    ]
  }
}
```

Key differences for a wrapped agent:

- `windows_file` is the **Python** exe, not the CLI exe.
- The **real** codex/claude exe goes at the tail, after `--`, with its
  base launch args. The wrapper appends the per-turn streaming/session
  args itself.
- **No `{SESSION_ARGS}` token** — the wrapper owns session continuity
  end to end (it persists and reloads the Codex `thread_id` / Claude
  `session-id` across its own turns *and* across a supervisor relaunch).
- No activity hook needed: a wrapped agent records its lifecycle and
  accepted CLI adapter events by construction.
- A work-heartbeat timer is coordination visibility, not CLI progress.
  It never advances `progress_sequence` and cannot keep a dead or wedged
  CLI brain green.
- A confirmed dead CLI brain requires two polls for the same wrapper and
  turn generation outside spawn/handoff grace. A stalled live brain also
  requires two confirming polls. Progress staleness becomes non-green at the
  configured threshold, but recovery also requires the full durable-write
  coalescing allowance and either a stale authoritative heartbeat or a live
  per-turn watchdog whose deadline-plus-margin floor is satisfied. Unknown
  evidence and fresh-heartbeat progress staleness alone never authorize a kill.
- **`--disable hooks`** on the wrapped **codex** child (the safe default):
  the wrapper owns the heartbeat, so the codex activity hook is neither
  needed nor wanted, and disabling it sidesteps codex's hook-trust prompt
  on every launch. Drop it from the tail only if you intentionally want
  the child's project hooks.

> **Per-CLI progress thresholds.** A wrapped **Claude** streams thinking,
> text, and tool deltas, so its default `stuck_after_seconds` is 180s. A
> wrapped **Codex** is item-level and can remain silent during pure
> reasoning, so its default is 2400s. Keep the value above both the
> longest legitimate event gap and, when the turn watchdog is effectively
> live, its resolved deadline plus the 300s margin. The value is never
> silently coerced; a threshold below a live watchdog's hard floor cannot
> authorize autonomous stall recovery.

> **An interpreter-option prefix before `-m agenttalk` (wrapped agents
> only).** If `windows_args` needs a Python interpreter flag ahead of
> `-m agenttalk` (e.g. `-u` for unbuffered output), declare exactly how
> many tokens that prefix occupies with `"launch": {"module_args_from": N}`
> — the supervisor never infers this from the argv text. Each prefix token
> must be a single dash-prefixed flag; **attached-value forms are
> accepted, separate-token values are refused**:
>
> ```jsonc
> "launch": {
>   "windows_args": ["-Xutf8", "-m", "agenttalk", "--root", "{ROOT}", "wrap", "..."],
>   "module_args_from": 1
> }
> ```
>
> `-Xutf8` (attached) works; `-X utf8` (separate tokens) does not — the
> bare value token `utf8` is indistinguishable from a script path and is
> refused along with it. **Separate-token forms are not supported at
> all — do not work around this by moving the flag after `-m agenttalk`:
> `-m mod` ends Python's own option list, so anything placed after it
> becomes agenttalk's argv, not Python's, and agenttalk's own argument
> parser will reject it.** Where CPython exposes an environment-variable
> equivalent (`PYTHONUTF8`, `PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE`,
> etc.), set that in the agent's `env` block instead — it has the same
> effect without occupying an argv position at all. Omit
> `module_args_from` entirely for the common case (no interpreter
> prefix, the default).

---

## 5. Install the project hooks

A manual-listen agent only becomes stuck-recoverable once it can prove
it is working. Install the project hooks, then flip `activity_hook` for
manual-listen agents:

```powershell
agenttalk supervise --install-activity-hook          # Claude heartbeat + checkpoint hooks
agenttalk supervise --install-activity-hook --codex  # also the Codex heartbeat hook
agenttalk supervise --install-activity-hook --interactive-for claude-dev
```

For Claude, this **merges** three identity-neutral hooks into the project
`.claude/settings.json`: the heartbeat `PostToolUse` hook, a `PreCompact`
checkpoint save hook, and a `SessionStart` hook with matcher `compact` that
resumes the checkpoint. The `--codex` and `--codex-only` paths add only the
heartbeat `PostToolUse` hook to `.codex/hooks.json`; wrapped Codex checkpoint
integration is separate. The installer is never global and never clobbers
unrelated settings. The hooks use `AGENTTALK_SELF`, so supervised/manual
agents launched with that environment act as themselves. Then set
`"activity_hook": true` on a manual-listen agent's block to enable
stuck-recovery.

Use `--interactive-for <lead>` only for the human's current
operator-facing Claude liaison window. It writes the three Claude hooks with
an explicit fallback identity for a window that does not have
`AGENTTALK_SELF`; `AGENTTALK_SELF` still wins when present. Non-liaison
interactive windows should set `AGENTTALK_SELF` instead. This is still honest
liveness: if the heartbeat is stale or missing, lead-chat remains unavailable
and the dashboard shows the lead as away. Wrapped Claude agents inherit the
project checkpoint hooks and identify themselves through `AGENTTALK_SELF`;
their working-turn heartbeat still comes from the wrapper.

---

## 6. Run the monitor

Select and record the PowerShell Core host first. Automatic selection considers
only canonical Program Files installations; PATH entries are diagnostics, not
execution candidates.

```powershell
$pwshPath = (agenttalk supervise --select-pwsh | ConvertFrom-Json).path
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor.ps1
```

To select a portable or nonstandard installation, use an absolute path:

```powershell
$pwshPath = (agenttalk supervise --select-pwsh `
  --pwsh 'D:\Tools\PowerShell\pwsh.exe' | ConvertFrom-Json).path
```

An explicit candidate is terminal: a missing, inaccessible, wrong-edition, or
changed file fails that request instead of falling back. The project record and
native file checks provide same-user consistency, not executable signing or ACL
attestation; every generated script also keeps its in-process Core/major guard.

The monitor loops every `poll_seconds`: it invokes the internal executable poll
for the decision table and executes it — launching agents that aren't running,
relaunching (resuming each agent's session — see section 1) agents whose
heartbeat went stale past grace, killing only the scoped process tree it manages,
and warning (never killing) for un-instrumented stale agents. The public
`agenttalk supervise --plan` command is only the read-only preview shown below.

Leave it running. It survives the agents crashing; the agents survive it
restarting.

### Watch what it sees

In another terminal:

```powershell
agenttalk supervise --report   # read-only liveness JSON: per-agent fresh/stale, threshold
agenttalk supervise --plan     # non-advancing decision preview, with no kill authority
agenttalk dashboard            # browser view: heartbeat age, who's composing, open threads
```

`--report` and `--plan` are pure read-only derivations — safe to run any time.
They neither change state nor earn a confirming poll. The executable form accepts
only a process whose live ancestry and host identity match the current selected
PowerShell marker; the instance token alone is not authority. This proves the
caller/host relationship, not the generated script's provenance (see #131).

### Use the Scheduled Task host

For durable logon hosting, run the task helper through the selected host:

```powershell
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor-task.ps1 -Action install
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor-task.ps1 -Action start
```

The task action freezes that absolute path. It is compared to the project
selection before start and is never executed as a discovery/probe candidate.
`status`, `stop`, and `uninstall` remain available to recover a stale binding.

To change the selected task host, stop it, wait until task status is no longer
`Running` and the old supervisor process has exited, uninstall it, select the
new absolute host, refresh/install, then start. The full copy-paste sequence is
in [Supervisor Hosting](supervisor-hosting.md#scheduled-task).

---

## 7. Restart an agent on demand (with context)

To bounce an agent yourself — say it wedged, or you want it to reload
after you changed something:

```powershell
agenttalk request-restart --for codex-dev --reason "reload after config change"
```

This writes a small restart-request marker. On its next poll the monitor
relaunches `codex-dev` **resuming its session** (the exact mechanism
depends on the agent — see section 1; for a manual Codex this is
`resume --last` in its `CODEX_HOME`), then clears the marker. The agent
comes back remembering its prior turn — the branch, the files it had open,
the work in flight. (Restarting a *protected* agent — see below — needs
`--force-protected`.)

---

## 8. The progress wrapper, standalone

You can run the wrapper **without** the supervisor, to get live
visibility and a working-turn heartbeat for a single agent:

```powershell
# Codex:
agenttalk wrap --for codex-dev --cli codex --loop -- `
  "C:\path\to\codex.exe" -a never -s workspace-write -C "D:\Projects\example"

# Claude:
agenttalk wrap --for claude-dev --cli claude --loop -- `
  "C:\Users\you\.local\bin\claude.exe" --permission-mode bypassPermissions
```

The wrapper:

- **gives visibility** — it echoes the agent's structured stream to the
  console (token/thinking deltas for Claude; item-level events for
  Codex), so a supervised agent is no longer a black box (`--no-render`
  to silence it).
- **heartbeats while working** — not just while idle, so a long, honest
  work turn never looks stuck.
- **detects degraded output** — a confirmed garble-then-silence pattern
  can request a restart of itself (recorded as `--from`).
- **owns one turn per inbound message** — it idles on the bus, and when a
  message lands it drives the CLI through exactly one turn, then returns
  to idle, persisting the session id so the next turn (or a relaunch)
  resumes.

`--loop` is what makes it the long-running supervised wrapper. Without
`--loop` it wraps a single one-shot invocation.

---

## 9. Migrating an existing project in and out of supervision

Supervision is **additive and reversible**. The bus — your messages,
roster, cursors, threads, and session history — is never touched by
turning supervision on or off. There is no data migration, no `reset`,
no re-`init`. You are only adding (or removing) an external monitor and a
few optional config files. Upgrading an already wrapped fleet from legacy
process ownership is the attended exception described below; it still does not
rewrite bus data.

### Adding supervision to a project you already run by hand

You already have a working store and have been running agents
interactively (`agenttalk init` done long ago, agents in
`/agenttalk.listen`). To put them under supervision:

1. **Scaffold** from the project root: `agenttalk supervise --init`. This
   writes `supervisor.json`, `supervisor.ps1`, and the `bin/agenttalk.cmd`
   shim (see section 3). Your `.agenttalk/` messages, roster, and state
   are untouched.
2. **Fill `supervisor.json` with the agents you already have.** Reuse the
   exact roster names (`agenttalk roster` to confirm) and the project path
   as `cwd`. Per agent, decide: keep it a manual-listen agent (then install
   the activity hook, step 5) or run it wrapped (step 4's wrapped
   archetype). You don't have to convert every agent — see "mixed mode".
3. **Stop your hand-run listener terminals** for the agents you're now
   supervising — the supervisor will launch them. Leave any agent you want
   to keep driving by hand alone.
4. **Select and run the monitor**: record the Core host with `supervise
   --select-pwsh`, then invoke `supervisor.ps1` through the returned absolute
   path as shown in section 6. It launches the
   configured agents (fresh on first launch) and keeps them alive from
   then on.
5. **Run the bootstrap preflight**: `agenttalk supervise --bootstrap-check`.
   Treat agents as assignable only after the JSON has no `error` checks.
   Warnings for stale roster-only names mean those identities are not live
   teammates yet; supervise them, retire them, or deliberately ignore them.
   The check is neutral for wrapped Claude and wrapped Codex agents and also
   catches missing explicit wrapped `--root` launch arguments.

That's the whole migration. Because the store is shared, a supervised
agent and the rest of your team see the same bus — a supervised
`codex-dev` and a hand-run `claude-rev` message each other normally.

> **One window per agent still holds.** Don't leave a hand-run
> `/agenttalk.listen` for `codex-dev` open *and* have the supervisor launch
> `codex-dev` — that's two consumers on one mailbox (unsupported; see the
> README "one window per agent" note). Pick one driver per agent.

### Upgrading a legacy wrapped fleet

A legacy `managed_pids` record cannot prove the whole owned tree because the
old traversal stopped at shell hosts. The supervisor retains that evidence as
a nondismissible `process_tree_hold`; it does not use it to kill.

1. Leave `.agenttalk/supervisor.kill` present, stop the supervisor, and confirm
   the strict instance marker is absent.
2. Read `agenttalk attention` and record the HOLD's `source_hash` and launch
   nonce. Inventory the full process tree. Before stopping the wrapper, re-read
   `--supervisor-launch-nonce` from its live command line and verify it matches
   the recorded nonce; after teardown, verify every recorded PID/start identity
   is absent or definitely recycled. If the wrapper is no longer live enough
   to re-read its nonce, use manual repair instead of this reset.
3. Run the reset as the operator-facing liaison (or sole lead):

```powershell
agenttalk supervise --reset-process-tree-ownership --from <liaison> `
  --for <agent> --hold-source-hash <64hex> `
  --verified-launch-nonce <verified-launch-nonce> `
  --acknowledge-no-live-supervisor `
  --acknowledge-owned-processes-stopped `
  --reason "attended owned-tree migration"
```

The reset is hash-, nonce-, role-, marker-, kill-switch-, strict-runtime-, and
PID/start-checked. It never kills or launches; it revokes stale ownership
evidence and appends a bounded audit entry. A stale hash, missing/mismatched
nonce, invalid or mismatched runtime wrapper identity/generation, live or
unverifiable identity, or unauthorized actor refuses the operation. If the
HOLD has no nonce/reset evidence, manual state repair is required. The reset
also atomically retires the exact old runtime digest and its
PID/start/generation/nonce boundary. Only that unchanged sidecar is ignored;
changed or new-generation runtime evidence still follows fail-closed adoption.

4. Keep the supervisor host stopped, remove `supervisor.kill`, and run
   `agenttalk supervise --refresh-scripts` (`--refresh-scripts` refuses while
   the kill switch is present). Queue
   `agenttalk request-restart --for <agent>`, then resume the supervisor. The
   next launch must earn a new wrapper generation and complete tree.

### Mixed mode

You can supervise some agents and run others by hand in the same store.
Only the agents listed in `supervisor.json` are managed; everyone else is
ignored by the monitor. This is the normal path while you try supervision
on one agent before trusting it with the whole team.

### Backing out — returning to attended/interactive

To stop supervising, **stop running `supervisor.ps1`** (Ctrl-C its
terminal). That is the only required step — the supervisor is just an
external monitor, so once it's gone nothing auto-restarts anything. Your
agents and bus are unaffected. Relaunch any agent you want to drive by
hand with `/agenttalk.listen` as before.

Optional cleanup — none of it required, all of it inert when the monitor
isn't running:

- **`supervisor.json`, `supervisor.ps1`, `bin/agenttalk.cmd`, and
  `supervisor-state.json`** can all stay; the config/script/shim do
  nothing unless you run the monitor, and `supervisor-state.json` is just
  the monitor's bookkeeping. Delete any of them if you prefer a clean
  tree (none is bus state).
- **The activity hook** (manual agents) is a harmless heartbeat stamp on
  every tool call. To remove it, delete the `agenttalk heartbeat` entry
  the install merged into the project `.claude/settings.json` (and
  `.codex/hooks.json`) — there's no auto-uninstall, and it was a careful
  merge, so remove only that entry.
- **Unattended permission seeding.** Supervision seeds agents into a
  never-prompt mode. To get approval prompts back for attended use, revert
  the seeded Claude `defaultMode` in the project `.claude/settings.json`
  (set it back to `default` or remove the key), and for Codex either
  relaunch against your normal `CODEX_HOME` instead of the seeded isolated
  one, or run `agenttalk codex-config --disable` if you had enabled the
  per-project sandbox block.

### What survives either direction

Bus state — heartbeats, cursors, threadstate — lives under
`.agenttalk/state/` and is mode-agnostic, valid whether or not a monitor
is running. Session *continuity* is held differently per path (a pinned
Claude id in `supervisor-state.json`; a manual Codex's `resume --last`
against its `CODEX_HOME`; a wrapped agent's own persisted id/`thread_id`),
but none of it is invalidated by switching modes. So you can flip back and
forth — supervise an overnight run, return to interactive in the morning —
without ever rebuilding state.

---

## 10. Safety and known limitations

- **Protected agents are never auto-killed.** The operator-facing
  liaison and every active `role=lead` agent are protected: the
  supervisor warns/notes but does not kill them, and a manual
  `request-restart` of one needs `--force-protected`. You do not want
  your one human-facing voice silently bounced.
- **Loopback-only observability.** `agenttalk dashboard` /
  `agenttalk serve` bind `127.0.0.1` only — there is no remote-bind flag.
  SSH-tunnel the port if you need it from another machine. See
  `SECURITY.md`.
- **Unattended trust.** Seeded `bypassPermissions` / `approval_policy=never`
  means the agents act without asking. Scope `writable_roots` to the
  repo and only supervise projects you trust the agents to change.
- **Wrapped-Codex threshold is conservative by design.** Because Codex's
  stream is silent during pure reasoning, the progress threshold is loose
  (2400s by default) to avoid false-killing a thinking agent. A genuinely wedged
  Codex turn therefore takes that long to be caught. This is an
  operational false-positive tradeoff, not a provable bound.
- **Poison-message recovery.** If an inbound message reliably crashes a
  wrapped agent's turn, the runtime record exposes `TURN_FAILED`; retry and
  recovery still honor the existing backoff and readiness caps. Once the
  wrapper dead-letters the message, use
  `agenttalk dead-letter list`, `show`, `requeue`, or `resolve` to inspect and
  make an operator decision without rewinding cursors.
- **Upgrade is fail-safe.** Owned-tree validation precedes restart-marker and
  child-liveness policy. An invalid or truncated tree reports
  `PROCESS_TREE_INVALID` or `PROCESS_TREE_TRUNCATED`, grants no kill authority,
  and leaves the restart marker unconsumed. If no tree HOLD applies, an older
  wrapper without `wrapper-runtime.json` reports `CLI_CHILD_UNKNOWN`, even with
  a fresh heartbeat. Use the attended migration sequence above; a plain
  refresh/restart cannot clear legacy ownership evidence.
- **Windows launcher-lifetime proof is nullable.** `cli_launcher_lifetime` is
  either null or a complete positive-decimal `GetProcessTimes` creation/exit
  interval. Authoritative `complete`/`absent` Windows tree entries require a
  positive decimal `start_filetime`. `invalid`/`truncated` HOLD entries may
  retain null so failure evidence stays readable, but null grants no identity
  authority. Linux boot-ID/start-ticks tokens are exact without FILETIME. If a
  prior identity recorded a FILETIME, a current row missing it is ambiguous. A
  prior complete tree bridges an exited intermediate only for the same wrapper
  generation and launch nonce, with the exact previously recorded child and
  parent edge. New or reparented children fail closed.
- **Generation-bound waiter teardown.** A wrapper loop writes a unique token in
  its waiting marker and clears only that token in `finally`. An old wrapper
  therefore cannot erase a replacement marker. This protects observability; it
  does not make duplicate consumers supported.
- **Windows watchdog termination is narrower, not complete process hardening.**
  The per-turn watchdog uses `os.kill(pid, signal.SIGTERM)` and does not launch
  `taskkill.exe`; on Windows that is abrupt termination and eliminates that
  popup-producing subprocess path. The production reporter's desktop-heap
  diagnosis is plausible, not upstream-confirmed. Windows snapshot and
  start-time helpers run CIM through the selected Core host; selection, TTL, or
  native-identity ambiguity returns unavailable and never kills. A PID can be
  reused after the separate recheck, and leaf-first snapshot termination is not
  an atomic tree kill.
- **Pinned executables.** `windows_file` must be the real CLI exe (or
  Python for wrapped), never a `.cmd`/npm/PowerShell shim — a shim hands
  off and exits, and the supervisor would track the wrong process.
- **Don't host the whole fleet in one Windows Terminal.** A single
  `WindowsTerminal.exe` hosting every agent tab plus the supervisor is a single
  point of failure: one WT crash (e.g. an access violation in its render DLL)
  kills every hosted console at once, which presents as "all CLIs crashed
  simultaneously" even though agenttalk is fine (state and threads are durable on
  disk; the wrap `python.exe` processes survive with orphaned CLI children). Prefer
  separate hosts, or a Scheduled Task host (see `supervisor-hosting.md`), and pin a
  known-good WT build.
- **Forensics gotcha: a process query matches its own command line.** When you
  hunt for a stray supervisor with
  `Get-CimInstance Win32_Process | Where CommandLine -like '*supervisor.ps1*'`, the
  query process itself matches, so it can *look* like two supervisors are running.
  Check `ParentProcessId` (and start time) before concluding you have duelling
  supervisors.

---

## 11. Command reference

| Command | What it does |
| --- | --- |
| `agenttalk supervise --init [--force]` | Scaffold config plus four generated artifacts. `--force` refreshes generated files and preserves existing config/state. |
| `agenttalk supervise --select-pwsh [--pwsh ABSOLUTE_PATH]` | Probe and record the PowerShell Core 7+ host. An explicit candidate never falls through. |
| `agenttalk supervise --refresh-scripts [--pwsh ABSOLUTE_PATH]` | Regenerate/validate all four artifacts under the lifecycle lock; preserve config/runtime state. |
| `agenttalk supervise --repair-instance-marker --quarantine --acknowledge-no-live-supervisor` | Explicitly quarantine an invalid singleton marker after the operator confirms no supervisor is live. |
| `agenttalk supervise --reset-process-tree-ownership --from L --for A --hold-source-hash HASH --verified-launch-nonce NONCE --acknowledge-no-live-supervisor --acknowledge-owned-processes-stopped --reason TEXT` | Record an attended owned-tree boundary. Requires liaison/sole-lead authority, the kill switch, no live instance marker, the current Attention hash and nonce, and every recorded PID/start proven gone or recycled. Never kills or launches. |
| `agenttalk supervise --report` | Read-only per-agent liveness JSON (fresh/stale + threshold). |
| `agenttalk supervise --plan` | Read-only, non-advancing decision preview; contains no kill authority. |
| `agenttalk supervise --install-activity-hook [--codex\|--codex-only]` | Merge the identity-neutral heartbeat `PostToolUse` plus checkpoint `PreCompact` and `SessionStart/compact` hooks into the **project** `.claude/settings.json`. Codex modes write only the heartbeat hook to `.codex/hooks.json`. Never global, never clobbers unrelated settings. |
| `agenttalk supervise --install-activity-hook --interactive-for <lead>` | Merge the three Claude hooks with a fallback identity for the current operator-facing human liaison; `AGENTTALK_SELF` still takes precedence. Refuses Codex hook modes. |
| `agenttalk wrap --for A --cli claude\|codex [--loop] [--no-render] -- <real exe> <base args>` | Run an agent through the progress wrapper: visibility + working-turn heartbeat + degraded detection. `--loop` = long-running supervised wrapper, one turn per inbound message. |
| `agenttalk request-restart --for A [--reason ...] [--force-protected] [--acknowledge-live-protected-kill]` | Queue a manual restart (resumes the session - mechanism per section 1). Healthy idle agents are restarted at the next supervisor poll. `--force-protected` restarts a protected agent; add `--acknowledge-live-protected-kill` when that protected agent still has a fresh heartbeat. |
| `agenttalk heartbeat [--for A] [--min-interval 5]` | Stamp the activity heartbeat (wired as a hook for manual agents; the wrapper does this for you). Hook identity comes from `--for`, then `AGENTTALK_SELF`; the interactive installer uses a hook-only fallback for the liaison window. Throttled, so the per-tool-call hook is nearly free. |

For the full per-agent config schema, read the generated
`supervisor.json` — every field carries an inline `_comment_*` explaining
it.
