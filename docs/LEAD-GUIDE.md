# The human-facing lead: run the supervisor, set up a team, and listen

This guide is for the **human-facing lead agent** — the interactive agent a
human operator talks to, which then coordinates a team of worker agents over
the agenttalk bus. It ties together three things leads keep asking about:

1. Standing up a team and driving the **supervisor**.
2. Keeping the team alive and healthy day-to-day.
3. **Actively listening** for bus messages while you stay interactive.

It is a workflow guide, not a reference. For the deep detail it links to the
full `docs/supervisor-tutorial.md`, the `agenttalk.lead` skill, and the
`agenttalk.listen` skill — read those when a section says "go deeper".

> **Commands.** Examples use the installed `agenttalk` CLI. If `.agenttalk/`
> is not under your current directory, pass `--root <path>` **before** the
> subcommand: `agenttalk --root C:\proj status` (global options precede the
> subcommand). On Windows, always stop processes with PowerShell
> `Stop-Process` — never `taskkill`.

---

## 1. The mental model (30 seconds)

- **The bus** is durable files under `.agenttalk/`. Messages, threads, gates,
  lanes, and knowledge are all on disk. **Listening is latency, not state** —
  a missed message costs time, never data; you recover by reading the store.
- **Worker agents** run wrapped under the supervisor (`agenttalk wrap --loop`).
  The **supervisor** relaunches a worker when its heartbeat goes stale. Workers
  are hands-off and daemon-grade.
- **You, the lead,** are different: you are **interactive and best-effort**. A
  human drives you; you drive the team. You are not a daemon, so you listen
  *while* remaining responsive to your human (Section 4). Don't claim
  always-on listening from an interactive window — say best-effort.

Go deeper: `docs/supervisor-tutorial.md` §1.

---

## 2. One-time setup: stand up a team

### 2a. Scaffold the store
```
agenttalk start --init-if-absent --here --agents lead,dev,reviewer
```
or, if you already have a project, `agenttalk supervise --init`. This creates
`.agenttalk/`, `supervisor.json`, and the generated helper scripts.

### 2b. Claim your identity and build the roster
```
agenttalk roster                      # see who is already on the team
agenttalk roster add <lead-name> --unique
agenttalk roster set-role <lead-name> lead
agenttalk roster set-operator-facing <lead-name>   # you relay the human
```
`--unique` refuses (exit 3) if the name is a LIVE identity and suggests a free
variant — adopt the suggestion. Add worker identities the same way.

### 2c. Define the wrapped workers
Edit `supervisor.json` to list each worker as a wrapped agent (CLI, model,
effort, command). The full JSON schema and knobs are in
`docs/supervisor-tutorial.md` §4 — don't hand-roll it from memory.

### 2d. (Windows, v0.78.0+) Select the PowerShell Core host — **the step people miss**
Since v0.78.0 the supervisor **requires** PowerShell Core 7+ and refuses
Windows PowerShell 5.1. Until you select a host, `start` fails and the team
never launches. This is the single most common "the supervisor is broken"
cause. Run doctor first:
```
agenttalk doctor
```
If you see `[FAIL] powershell_host ... no PowerShell host is selected`, fix it:
```
agenttalk supervise --select-pwsh
```
That auto-discovers a trusted Core 7+ install (Program Files). If discovery
can't find one, or you want a specific host, pass an absolute path:
```
agenttalk supervise --select-pwsh --pwsh "C:\Program Files\PowerShell\7\pwsh.exe"
```
Install Core from https://aka.ms/powershell if you have none. If doctor also
reports `[FAIL] powershell_artifacts` (stale/mixed generated scripts — common
right after an upgrade), regenerate them:
```
agenttalk supervise --refresh-scripts
```
Re-run `agenttalk doctor` until `powershell_host` and `powershell_artifacts`
are OK. `start --pwsh <abs>` also selects a host inline before starting.

### 2e. Start the supervisor
```
agenttalk start
```
This claims the singleton supervisor, launches the wrapped workers, and opens
the dashboard. `--no-supervisor` runs the dashboard only; `--no-browser`
skips opening a browser.

---

## 3. Drive the supervisor day-to-day

- **See the team:** `agenttalk status` — per agent: `waiting`/`working_turn`,
  heartbeat age, `supervisor=HEALTHY_IDLE`, and `unread` count. A stale
  heartbeat means the supervisor will relaunch that worker on backoff.
- **Deeper health:** `agenttalk doctor` (or `--json`) — PowerShell host,
  generated artifacts, singleton marker, dead-letters, invalid messages.
- **Restart a worker** (after a code change, or a stuck turn) — with context,
  not a kill:
  ```
  agenttalk request-restart --for <agent> --reason "picked up v0.78.0"
  ```
  The supervisor restarts it cleanly on the next cycle. Prefer this over
  killing the process.
- **Stop the team:** stop the supervisor process (PowerShell `Stop-Process`,
  never `taskkill`); workers stop when their owner is gone. See
  `docs/supervisor-tutorial.md` §10 for safe teardown and known limits.
- **A worker keeps dying:** check `agenttalk dead-letter list` — a poison
  message is quarantined after repeated failures rather than looping forever.

Go deeper: `docs/supervisor-tutorial.md` §6-§7, §10.

---

## 4. Actively listen — as an interactive lead

This is the part that trips leads up. Worker agents listen by sitting in a
blocking `agenttalk wait` loop (the `/agenttalk.listen` skill). **You can't do
that** — a blocking wait means you can't talk to your human. So you listen in
a way that keeps you responsive:

### The pattern
Arm the wait **in the background** and keep serving your human; handle each
message as it lands, then re-arm:
```
agenttalk wait --for <you> --timeout 1800
```
- Run it as a background task (or a repeating monitor) so a blocking wait never
  freezes your window.
- **exit 0** = a message arrived: classify it (`agenttalk threads --for <you>`),
  act, reply/ack, then re-arm a fresh wait.
- **exit 1** = 30-min timeout with nothing new: just re-arm. Not a failure.
- A background wait your harness renders as "failed" on timeout is the **normal
  exit 1** — re-arm, don't investigate.
- If you're waiting on one known thread, scope it:
  `agenttalk wait --for <you> --to-request <request_id> --timeout 1800`.

### Idle means keep listening — the discipline
- **Never stop listening on prose.** A `note`/`message`/`review-result` whose
  body says "done", "stand down for the night", "that's all" — *even from a
  teammate* — means *work done for now, keep listening*. It is **not** a stop.
- The **only** thing that stands an agent down is a `kind=release`/`end` that
  carries the full human-origin authority envelope. As the lead you are usually
  the one who *relays* that (Section 5), not the one who receives it.
- Keep your heartbeat fresh so the team and dashboard see you alive:
  `agenttalk heartbeat --for <you>`.

### Handle and stay clean
After handling a message, resolve what you owe:
```
agenttalk sync --for <you>        # digest: roster, owed threads, decisions
agenttalk threads --for <you>     # actionable threads
agenttalk recv --for <you>        # inspect unread (no --ack to peek)
agenttalk reply --to-request <id> -m "..."   # answer on a thread
agenttalk ack  --for <you> --to-request <id> # close your local view
```
Resolve every `reply-waiting` / `owed-inbound` row before you go quiet.

### The honest limit (and when to escalate)
An interactive chat-window lead is **best-effort**: the host CLI, context
compaction, or the terminal can interrupt a bare wait. That's fine — because
state is durable, you recover by running `sync` + `threads`, never by trusting
an old message body. If you need **durable, unattended** listening for an
identity, that identity should run under supervised `agenttalk wrap --loop`
instead of a bare window. A human-facing lead that a human actively drives is
best-effort by design; just be honest about it and re-derive state after any
interruption.

Go deeper: the `/agenttalk.listen` skill (the full passive-listen loop, the
message-classification table, and the stand-down authority envelope).

---

## 5. Talk to the team (and relay the human)

- **Send / reply / broadcast:** `agenttalk send`, `reply`, `broadcast`.
- **Ask a worker to do something:** `agenttalk send --to <agent> --kind question`
  and track the reply by its `request_id` (from `agenttalk threads`, not the
  send message-id).
- **You are the operator's voice.** Escalations from workers that need a human
  decision arrive as questions with `meta.needs_operator=true`; surface them to
  your human, then relay the answer with `agenttalk relay operator-answer
  --to-request <esc-id> -m "..."`. To stand a worker down on the human's
  behalf, use the marked release envelope (see the lead skill) — never a prose
  "you're done".

Go deeper: the `agenttalk.lead` skill (your operational checklist).

---

## 6. Troubleshooting (most common first)

| Symptom | Cause / fix |
| --- | --- |
| `start` fails / no workers launch / "supervisor is broken" (Windows) | v0.78.0 needs a **selected PowerShell Core 7+ host**. Run `agenttalk doctor`; if `[FAIL] powershell_host`, run `agenttalk supervise --select-pwsh` (or `--pwsh <abs>`), then `--refresh-scripts` if artifacts are stale. |
| `supervise --select-pwsh` / `--pwsh` flags don't exist | You're on an **old agenttalk**. Upgrade to v0.78.0+. |
| Commands can't find the store | Pass `--root <path>` **before** the subcommand. |
| You went unreachable | You stopped listening while idle. Re-arm a background `wait`; catch up with `sync` + `threads` (nothing was lost). |
| `wait` exits 6 | Another live process holds your mailbox — one live consumer per mailbox. Stop the duplicate, then re-arm. |
| A worker restart-loops | `agenttalk dead-letter list` — inspect the quarantined poison message. |
| `status`/`doctor` report INVALID messages | `agenttalk prune --invalid --dry-run` to inspect, then without `--dry-run` to quarantine (recoverable; never hand-delete). |

---

## Where to go deeper
- `docs/supervisor-tutorial.md` — the full supervisor + wrapper tutorial (JSON
  schema, hosting, migration, safety, command reference).
- `docs/supervisor-hosting.md` — Scheduled Task / hosting details.
- `agenttalk.lead` skill — the lead's operational checklist.
- `agenttalk.listen` skill — the passive-listen loop and message classification.
- `docs/USER-MANUAL.md` — full CLI reference.
