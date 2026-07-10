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

1. **Heartbeat freshness is the liveness authority.** Each agent stamps
   a small `heartbeat` file as it works and while it idles in `wait`. A
   **fresh** heartbeat means healthy — even if the supervisor can't find
   the process. A **stale** heartbeat (older than `stuck_after_seconds`)
   is the only signal that triggers recovery. There is no fragile
   "find the right PID" dance deciding life-or-death.

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
  which owns the idle wait and heartbeats by construction (plus streams
  the agent's progress to the console). No hook needed.

Until an agent can confirm "stuck" (hook installed **or** wrapped), a
stale heartbeat is **warn-only** — never a kill. An un-instrumented
agent is never mistaken for stuck.

---

## 2. Prerequisites

- agenttalk installed and a store initialized in your project
  (`agenttalk init --here --agents ...`). See the README quickstart.
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

This creates three things under `.agenttalk/` (use `--force` to
overwrite):

- **`supervisor.json`** — the config you fill in: cadence knobs plus a
  per-agent block describing how to launch each agent.
- **`supervisor.ps1`** — the generated PowerShell monitor. You run this;
  you do not edit it. It polls `supervise --plan` and executes the plan
  (launch / relaunch-with-resume / scoped kill / warn).
- **`bin/agenttalk.cmd`** — a tiny shim the monitor calls so its own bus
  commands resolve to the right Python/agenttalk regardless of your PATH.
  You don't invoke it directly.

Once you start the monitor it also writes **`supervisor-state.json`** —
script-owned bookkeeping (per-agent launcher pids, pinned Claude session
ids, backoff timers). That is the monitor's own state, **not** bus state,
and it's safe to delete while the monitor isn't running.

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
- No activity hook needed: a wrapped agent is instrumented by
  construction, so a stale heartbeat past grace **recovers** rather than
  warn-only.
- **`--disable hooks`** on the wrapped **codex** child (the safe default):
  the wrapper owns the heartbeat, so the codex activity hook is neither
  needed nor wanted, and disabling it sidesteps codex's hook-trust prompt
  on every launch. Drop it from the tail only if you intentionally want
  the child's project hooks.

> **Per-CLI stale thresholds.** A wrapped **Claude** streams thinking,
> text, and tool deltas, so it stays fresh through reasoning — default
> `stuck_after_seconds` 180s. A wrapped **Codex** is item-level: no
> event closes a long pure-reasoning gap, so the stream goes silent
> between turn start and the final message — default 900s, and for a
> heavy reasoning/review role pick 1200–1800s. **Guardrail:** a wrapped
> Codex with `stuck_after_seconds` below 600s degrades to warn-only
> (refuses restart authority) unless you set `allow_low_stuck_after=true`.
> The value is never silently coerced. Pick the threshold above the
> longest pure-reasoning gap your role plausibly hits.

---

## 5. (Manual agents only) install the activity hook

A manual-listen agent only becomes stuck-recoverable once it can prove
it is working. Install the heartbeat hook, then flip `activity_hook`:

```powershell
agenttalk supervise --install-activity-hook          # Claude PostToolUse hook
agenttalk supervise --install-activity-hook --codex  # also the Codex hook
agenttalk supervise --install-activity-hook --interactive-for claude-dev
```

This **merges** the identity-neutral `agenttalk heartbeat --hook` hook
into the project `.claude/settings.json` (and `.codex/hooks.json` with
`--codex`) — never global, never clobbering your existing settings. It
uses `AGENTTALK_SELF`, so it is correct for supervised/manual agents
launched with that environment. Then set `"activity_hook": true` on that
agent's block.

Use `--interactive-for <lead>` only for the human's current
operator-facing Claude liaison window. It writes a Claude-only hook with
an explicit fallback identity for windows that do not have
`AGENTTALK_SELF`. Non-liaison interactive windows should set
`AGENTTALK_SELF` instead. This is still honest liveness: if the
heartbeat is stale or missing, lead-chat remains unavailable and the
dashboard shows the lead as away.

Wrapped agents skip this entirely.

---

## 6. Run the monitor

```powershell
# from the project root, in a dedicated terminal:
.\.agenttalk\supervisor.ps1
```

The monitor loops every `poll_seconds`: it asks `agenttalk supervise
--plan` for the decision table and executes it — launching agents that
aren't running, relaunching (resuming each agent's session — see section
1) agents whose heartbeat went stale past grace, killing only the scoped
process tree it manages, and
warning (never killing) for un-instrumented stale agents.

Leave it running. It survives the agents crashing; the agents survive it
restarting.

### Watch what it sees

In another terminal:

```powershell
agenttalk supervise --report   # read-only liveness JSON: per-agent fresh/stale, threshold
agenttalk supervise --plan     # the action plan the script will execute
agenttalk dashboard            # browser view: heartbeat age, who's composing, open threads
```

`--report` and `--plan` are pure read-only derivations — safe to run any
time, they change nothing.

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
few optional config files.

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
4. **Run the monitor**: `.\.agenttalk\supervisor.ps1`. It launches the
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
  stream is silent during pure reasoning, the stale threshold is loose
  (900s+) to avoid false-killing a thinking agent. A genuinely wedged
  Codex turn therefore takes that long to be caught. This is an
  operational false-positive tradeoff, not a provable bound.
- **Poison-message recovery.** If an inbound message reliably crashes a
  wrapped agent's turn, the cycle is: fail -> heartbeat goes stale -> restart
  -> reload-resume -> re-deliver the same message -> fail again. Backoff
  throttles it (base..cap). Once the wrapper dead-letters the message, use
  `agenttalk dead-letter list`, `show`, `requeue`, or `resolve` to inspect and
  make an operator decision without rewinding cursors.
- **Pinned executables.** `windows_file` must be the real CLI exe (or
  Python for wrapped), never a `.cmd`/npm/PowerShell shim — a shim hands
  off and exits, and the supervisor would track the wrong process.

---

## 11. Command reference

| Command | What it does |
| --- | --- |
| `agenttalk supervise --init [--force]` | Scaffold `supervisor.json` + `supervisor.ps1`. |
| `agenttalk supervise --report` | Read-only per-agent liveness JSON (fresh/stale + threshold). |
| `agenttalk supervise --plan` | The action plan (decision table) the monitor executes. |
| `agenttalk supervise --install-activity-hook [--codex\|--codex-only]` | Merge the identity-neutral heartbeat PostToolUse hook into the **project** `.claude/settings.json` (and/or `.codex/hooks.json`). Never global, never clobbers. |
| `agenttalk supervise --install-activity-hook --interactive-for <lead>` | Merge a Claude-only heartbeat hook bound to the current operator-facing human liaison. Refuses Codex hook modes. |
| `agenttalk wrap --for A --cli claude\|codex [--loop] [--no-render] -- <real exe> <base args>` | Run an agent through the progress wrapper: visibility + working-turn heartbeat + degraded detection. `--loop` = long-running supervised wrapper, one turn per inbound message. |
| `agenttalk request-restart --for A [--reason ...] [--force-protected] [--acknowledge-live-protected-kill]` | Queue a manual restart (resumes the session - mechanism per section 1). Healthy idle agents are restarted at the next supervisor poll. `--force-protected` restarts a protected agent; add `--acknowledge-live-protected-kill` when that protected agent still has a fresh heartbeat. |
| `agenttalk heartbeat [--for A] [--min-interval 5]` | Stamp the activity heartbeat (wired as a hook for manual agents; the wrapper does this for you). Hook identity comes from `--for`, then `AGENTTALK_SELF`; the interactive installer uses a hook-only fallback for the liaison window. Throttled, so the per-tool-call hook is nearly free. |

For the full per-agent config schema, read the generated
`supervisor.json` — every field carries an inline `_comment_*` explaining
it.
