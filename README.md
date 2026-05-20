# agenttalk

A small file-backed message bus that lets two coding-agent CLIs — **Claude
Code** and **Codex** — work on the same project and message each other
directly. Built for the spec-driven workflow where one agent implements and
the other reviews.

The two agents share a project-local `.agenttalk/` directory; every message
becomes a small JSON file. Each CLI runs in its own terminal window so you
see the full conversation as it happens. A markdown transcript is exported
on session end.

---

## TL;DR — getting started

```powershell
# one-time install (anywhere)
git clone https://github.com/zoolok17/agenttalk.git
python -m pip install -e .\agenttalk
agenttalk install-skills         # copies skill files into ~/.claude/commands and ~/.codex/skills

# in your project root, once per project
agenttalk init --here --agents claude,codex
agenttalk codex-config --enable   # lets Codex call agenttalk from its sandbox
```

Open two terminals at the project root:

### For spec-kitty missions (the canonical workflow)

- **Terminal A (Claude Code).** `/agenttalk.sk-loop <mission-slug>`
- **Terminal B (Codex).** `$agenttalk-sk-loop <mission-slug>`

Each loop calls `spec-kitty next --agent <self>` to decide what to do
next, does the implement or review work in the persistent window, then
sends a tiny `kind=wake` message to the peer so they react instantly.
Both agents keep full context across all WPs — important for catching
cross-WP regressions a fresh subprocess reviewer would miss. spec-kitty
remains the source of truth for state; agenttalk is just a wake signal.

### For ad-hoc cross-agent messaging (not spec-kitty)

- **Terminal A (Claude).** `/agenttalk.handoff` (send + block on reply)
  or `/agenttalk.send` (fire and forget).
- **Terminal B (Codex).** `$agenttalk-listen` (wait/respond loop).

Both terminals show every message as it flies past. When you're done:
`agenttalk end --from claude --reason "done"` writes a markdown
transcript under `.agenttalk/sessions/`.

> **Naming convention:** Claude Code uses dotted skill names
> (`agenttalk.send`, `agenttalk.listen`, `agenttalk.handoff`,
> `agenttalk.sk-loop`) and Codex uses hyphenated names
> (`agenttalk-send`, `agenttalk-listen`, `agenttalk-handoff`,
> `agenttalk-sk-loop`). Behaviour is identical; only the slash-command
> spelling differs.

---

## Why this exists

The usual cross-agent workflow is:

1. You ask Claude to implement a feature.
2. You copy the diff into Codex and ask for a review.
3. You copy Codex's review back into Claude.
4. Repeat.

`agenttalk` removes the copy/paste. Claude implements, then runs
`/agenttalk.handoff` to ping Codex. Codex (in listen mode) wakes, reviews
the work, and replies. Claude wakes on the reply and either ships or
iterates. Both terminals display every message. You stay in the loop and
interrupt whenever you want.

---

## Install

```powershell
git clone https://github.com/zoolok17/agenttalk.git
python -m pip install -e .\agenttalk
```

This puts an `agenttalk` script on your PATH. The package is stdlib-only
(no third-party deps) and requires Python 3.10+.

If you also want Codex to call agenttalk from inside its sandbox, run
this once per project root:

```powershell
agenttalk codex-config --enable
```

It writes a per-project block to `~/.codex/config.toml` granting
`approval_policy = "never"` and `sandbox_mode = "workspace-write"` for
that project only. Reverse it with `agenttalk codex-config --disable`.

---

## One-time setup (per project)

From your project root:

```powershell
agenttalk init --here --agents claude,codex
```

This creates `.agenttalk/` with:

```
.agenttalk/
  config.json          session + agent roster
  messages/<id>.json   one file per message (chronologically sorted)
  state/<agent>.cursor last message id each agent has acknowledged
  sessions/            markdown/jsonl transcripts written by `agenttalk end`
```

Slash commands are installed globally (one-time, not per project) via
`agenttalk install-skills` — see the [Install](#install) section.

---

## Workflows

Open **two terminals** at the project root, one for each agent.

### Spec-kitty missions — `sk-loop` (recommended)

If you're running a spec-kitty mission, the persistent loop drives the
implement → review cycle automatically using `spec-kitty next` as the
state machine and a tiny `kind=wake` message for low-latency handoff.

```text
Terminal A (Claude):   /agenttalk.sk-loop <mission-slug>
Terminal B (Codex):    $agenttalk-sk-loop <mission-slug>
```

Both windows stay alive for the whole mission, accumulating full
context across every WP. Roles are symmetric — spec-kitty assigns
implement vs review per WP based on your `.kittify/config.yaml`.

### Ad-hoc cross-agent collaboration — `listen` + `handoff`

When agents are working together outside a spec-kitty mission (organic
work split, second opinions, cross-reviews of each other's work):

```text
Terminal A (Claude):   /agenttalk.listen          (passive: wait for peer)
Terminal B (Codex):    $agenttalk-listen          (passive: wait for peer)
```

Either side, when they finish a chunk and want it reviewed:

```text
/agenttalk.handoff       (Claude)  — bundles send + wait
$agenttalk-handoff       (Codex)
```

The handoff includes structured meta — `request_id`, `base_sha`,
`head_sha` — and a body template (Goal / Files changed / How to verify
/ Focus areas / Known caveats). The receiver mode-detects: if the
meta has a `mission` or `wp_id`, it runs the spec-kitty review path;
otherwise it does an ad-hoc cross-review of the named scope.

### Ending the session

```powershell
agenttalk end --from claude --reason "feature shipped"
```

This sends an `end` message to the other agent (breaking its listen
loop) and writes `transcript-<session_id>.md` under
`.agenttalk/sessions/`.

---

## Cost notes — listen mode is not free

> All numbers below are **rough estimates as of 2026-05**. Model pricing
> changes; treat these as order-of-magnitude guidance, not invoices.

`agenttalk wait` itself uses no model tokens — it's a Python subprocess
polling the filesystem. But every time the subprocess returns to the
LLM (either with a real message OR after the timeout fires), the agent
re-reads the conversation context and decides what to do. That's the
token cost driver.

### Order-of-magnitude estimates

Assuming a 30k-token conversation context and Claude Opus 4.7 pricing
(~$15/M input, ~$75/M output; cached reads ~$1.50/M):

| Mode | Wait timeout | Idle wake-ups / hour | Per-agent idle cost |
| --- | --- | --- | --- |
| `/agenttalk.listen` (default 1800s) | 30 min | ~2 | ~$0.90/hour (cache cold each wake) |
| `/agenttalk.sk-loop` (30s) | 30 sec | ~120 | ~$5/hour (cached, within 5 min TTL) |

Two agents listening in parallel → roughly double. Real messages are
handled immediately regardless of timeout (the subprocess returns the
instant a message file lands), so the table is **idle cost only**;
actual conversation rounds add their own per-message cost.

Codex has its own pricing model but the same architecture applies — a
short wait timeout means more idle wake-ups, each re-reading the
conversation. Configure accordingly.

### Why sk-loop pays more

The sk-loop's short timeout is doing real work: every cycle it ALSO
re-runs `spec-kitty next` to self-heal cases where the peer changed
state without sending a wake. Pure listen has no such second source,
so the long timeout is free observability.

### Reducing cost further

- `agenttalk wait --heartbeat-interval 0` disables heartbeat writes
  (saves trivial disk IO; no token impact).
- `agenttalk wait --timeout 0` blocks forever. Cheaper still — only
  real messages wake the LLM. Tradeoff: no safety-net liveness loop
  if the subprocess hangs.
- For long idle periods, just have the agent exit the loop and tell
  you when you're ready to resume. The `kind=end` message gracefully
  stops the other side.

---

## CLI reference

| Command | What it does |
| --- | --- |
| `agenttalk init [--here] [--agents A,B]` | Create `.agenttalk/` in the current dir. |
| `agenttalk status` | Show roster, per-agent cursor, unread count. |
| `agenttalk send --from A --to B [--kind K] [--subject S] [--meta k=v] -m "body"` | Drop a message into the bus. Body can come from `-m`, `--file`, or stdin. |
| `agenttalk recv --for A [--ack]` | Print all queued messages for an agent. |
| `agenttalk wait --for A [--timeout 120] [--no-ack]` | Block until a new message arrives, print it, advance the cursor. |
| `agenttalk ack --for A [--id ID]` | Manually move an agent's cursor forward. |
| `agenttalk transcript [--format md\|jsonl] [--out PATH]` | Export the full conversation. |
| `agenttalk end --from A [--reason ...]` | Notify the other agent(s) and write the transcript. |
| `agenttalk install-skills [--claude-only\|--codex-only] [--force] [--dry-run]` | Copy bundled skill files to `~/.claude/commands/` and `~/.codex/skills/`. Idempotent — preserves your local edits unless `--force`. |
| `agenttalk codex-config [--enable\|--disable\|--status]` | Manage per-project sandbox/trust block in `~/.codex/config.toml` so Codex can call agenttalk from inside its sandbox. |

Message `--kind` values are free-form, but the skills assume:

- `message` — generic chat
- `review-request` — "please review this WP"
- `review-result` — "I reviewed; here's my verdict" (use `--meta status=approved|rejected`)
- `question` — needs a reply before the other side proceeds
- `note` — informational
- `end` — terminate the listen loop on the other side

---

## How "both terminals see the message" works

There is no broadcast channel. Each `send` writes the message file
**and** prints the rendered message to the sender's stdout. The receiver's
`wait` (running in the other terminal) picks up the same file and prints
it on the other side. So both terminals show both halves of the
exchange — and the full conversation is on disk in `.agenttalk/messages/`
as the source of truth.

The transcript exporter walks `messages/` in id order (which is
chronological) and renders it as markdown.

---

## Design notes

- **No daemon.** Just files. Survives reboots, terminal crashes, agent
  restarts.
- **No append contention.** One JSON file per message, written atomically
  via temp-file + `os.replace` (atomic on NTFS and POSIX).
- **Cursor-per-agent, not per-message.** Reading is just "list messages
  newer than my cursor"; ack moves the cursor.
- **Polling, not watchers.** `wait` polls every 0.3s. Good enough for
  human-paced agent collaboration, works identically on Windows and
  POSIX, no extra dependencies.
- **No transport assumptions.** Both agents must run on the same machine
  (or share the project directory via any sync mechanism you already
  trust). That's the deliberate constraint that keeps the design tiny.

---

## When to ask the human

The listen-loop skill instructs each agent to stop and ask the human if
it hits something it can't decide alone (ambiguous review feedback,
unexpected test failure, scope creep). The human can also send a message
into the bus manually:

```powershell
agenttalk send --from human --to claude -m "stop, I want to change the spec"
```

`human` is not in the default agent roster — add it at init time with
`--agents claude,codex,human` if you want this.
