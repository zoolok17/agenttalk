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
# one-time install (canonical, tag-pinned)
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.8.0"
agenttalk install-skills          # copies skill files into ~/.claude/commands and ~/.codex/skills

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

- **Terminal A (Claude).** `/agenttalk.handoff` (send + block on reply),
  `/agenttalk.consult` (confer with peer before answering you), or
  `/agenttalk.send` (fire and forget).
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

1. You ask one agent to implement a feature.
2. You copy the diff into the other agent and ask for a review.
3. You copy the review back into the first.
4. Repeat.

`agenttalk` removes the copy/paste. The implementer runs
`/agenttalk.handoff` to ping the peer. The peer (in listen mode) wakes,
reviews the work, and replies. The implementer wakes on the reply and
either ships or iterates. Both terminals display every message. You stay
in the loop and interrupt whenever you want.

Which agent plays implementer vs. reviewer is your call — agenttalk is
symmetric. Claude-implements-Codex-reviews and Codex-implements-Claude-
reviews are equally supported; in a spec-kitty mission, spec-kitty
assigns the part per WP and the sk-loop skills follow.

---

## Install

**End users (canonical, tag-pinned):**

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.8.0"
```

Pin to a specific tag so you control upgrades. Replace `v0.8.0` with
whatever's listed on the [releases page](https://github.com/zoolok17/agenttalk/releases).
Check what you have with `agenttalk --version`.

**Contributors (editable clone):**

```powershell
git clone https://github.com/zoolok17/agenttalk.git
cd agenttalk
python -m pip install -e ".[dev]"   # includes pytest + build
```

The editable install means code changes are picked up live without
re-running pip.

Either path puts an `agenttalk` script on your PATH. The package is
stdlib-only (no third-party runtime deps) and requires Python 3.10+.

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

## Agent identity (and running two of the same kind)

Agent names are **safe identifiers** — alphanumeric plus dot,
underscore, or hyphen, starting with an alphanumeric, max 64 chars.
This restriction exists because names are interpolated into
`.agenttalk/state/<name>.cursor` (and similar) filenames; anything
that could escape that directory (path separators, `..`, leading
punctuation, quotes, whitespace) is rejected.

The default pair is `claude` and `codex`, but you can run two Claudes
(or two Codexes) by giving them distinct names like `claude-a` /
`claude-b` — useful if you don't have subscriptions to both tools.

Each terminal declares which agent it is via env vars:

```powershell
# Terminal A
$env:AGENTTALK_SELF = 'claude-a'
$env:AGENTTALK_PEER = 'claude-b'

# Terminal B
$env:AGENTTALK_SELF = 'claude-b'
$env:AGENTTALK_PEER = 'claude-a'
```

```bash
# Terminal A
export AGENTTALK_SELF=claude-a AGENTTALK_PEER=claude-b

# Terminal B
export AGENTTALK_SELF=claude-b AGENTTALK_PEER=claude-a
```

Initialize with matching names:

```powershell
agenttalk init --here --agents claude-a,claude-b
```

All `agenttalk` commands accept `--from`/`--to`/`--for` flags as
overrides. If the flags are absent, the CLI uses the env vars; if
**both** are absent the CLI exits with a clear error pointing you at
either the flag or the env var. The CLI does NOT silently assume
`claude`/`codex` — those defaults live only in the bundled skill
files (so an LLM running `/agenttalk.send` without env set still
works for the canonical pair).

The CLI also validates the resolved name against the roster: a typo
like `AGENTTALK_SELF=claud` exits 2 rather than silently operating
on a phantom mailbox. And it rejects self-mail (`SELF == PEER`).

`agenttalk init` prints concrete env-setup commands at the end of its
output (for 2-agent rosters), so you can copy-paste straight into each
terminal.

### Env caveat for LLM tool-call contexts

When you set `AGENTTALK_SELF` in your terminal profile or your shell
RC, every child process inherits it — including `agenttalk`
subprocesses spawned by the LLM, so things "just work".

But env vars set INSIDE an LLM tool call (e.g., `$env:AGENTTALK_SELF =
'claude-a'` in one PowerShell tool call) may NOT persist into the next
tool call, because each tool call is often a fresh shell process. The
bundled skill files resolve identity inside each tool call's shell, so
this is transparent in practice — but if you write your own
automation, set env in the parent shell or pass explicit
`--from`/`--to`/`--for` flags.

---

## Splitting implementation work between agents

Outside a spec-kitty mission, the skills tell each agent **not to
split implementation work with the peer without first asking you**.
The user invoked them to do a task; the peer is for review or specific
delegated subtasks, not for unilaterally carving up the work.

When you DO want them to split (e.g., "Claude does the frontend, Codex
does the backend"):

1. Say so explicitly. Each agent confirms the ownership boundaries via
   a `kind=note` message.
2. **Every implemented piece is then cross-reviewed** — the
   implementer of one chunk sends `kind=review-request` to the peer,
   who reviews read-only and replies with `kind=review-result`. This
   is mandatory in the skill bodies, not optional.
3. The implementer of each piece fixes their own blockers. Reviews
   never silently patch peer code.

In a spec-kitty mission, ignore the above — spec-kitty's state machine
assigns implement/review per WP, and the sk-loop skills do the right
thing automatically.

---

## Pre-answer consults — letting agents confer before responding

Sometimes you ask one agent a question and you want the other to
pressure-test the draft answer before it lands. The `/agenttalk.consult`
(`$agenttalk-consult` on Codex) skill does exactly that:

1. The receiving agent drafts an answer in private.
2. Sends the draft + its uncertainty to the peer via `kind=question`
   with `meta.consult=true`.
3. The peer reads it, critiques (blocking objections, missing
   assumptions, alternative recommendation), and replies with `agree
   / disagree / qualified-agree`.
4. The receiving agent synthesizes a concise final answer naming
   agreement, disagreement, and its recommendation. The peer never
   answers you directly and never modifies files.

When agents auto-trigger it (per the bundled skill bodies):

- **Always:** when you explicitly ask ("ask codex", "discuss first",
  "get a second opinion").
- **Default:** high-impact ambiguous calls — architecture,
  requirements, security tradeoffs, data-loss risk, irreversible
  workflow decisions, expensive implementation direction.
- **Skipped:** trivial questions, syntax, status updates, bounded
  reviews (those use `/agenttalk.handoff`), or anything the agent is
  confident about that you haven't asked for a second opinion on.

The freshness check (heartbeat older than ~5 min) suppresses the
consult and the agent tells you the peer wasn't listening so it
answered on its own. Default consult wait is 180 s, much shorter
than work-item handoffs — consults are interactive.

Cost: each consult is a full round-trip, so latency and tokens roughly
double for that exchange. Worth it for real decisions, wasteful for
trivia — the skill bodies have explicit guidance on when to fire.

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
| `agenttalk recv --for A [--ack] [--include-control]` | Print all queued messages for an agent. Hides `composing` pings by default; `--include-control` surfaces them. |
| `agenttalk wait --for A [--timeout 120] [--no-ack] [--grace 2] [--composing-extend 120]` | Block until a new message arrives, print it, advance the cursor. `--grace` does one final inbox scan after the deadline (catches replies that landed in the last fraction of a second). `--composing-extend` lengthens the deadline by N seconds for each `composing` ping the peer sends (capped at 1800 s total). |
| `agenttalk composing --from A --to B [-m "still drafting"]` | Send a `composing` ping so the peer's `wait` extends its deadline. Use periodically while you draft a long reply. The peer's `wait` consumes these as deadline-extension signals — they do NOT surface as a returned reply. |
| `agenttalk ack --for A [--id ID]` | Manually move an agent's cursor forward. |
| `agenttalk transcript [--format md\|jsonl] [--out PATH]` | Export the full conversation. |
| `agenttalk end --from A [--reason ...]` | Notify the other agent(s) and write the transcript. |
| `agenttalk reset [--archive]` | Clear **active bus state** (messages + cursors + heartbeats); preserves historical transcripts under `.agenttalk/sessions/` so past exports aren't lost. Bumps `session_id`. With `--archive`, instead moves **everything** (messages + state + sessions) under `.agenttalk/archived/<old_session>/`. Preserves config (roster) either way. |
| `agenttalk hmac-init [--force]` | Generate the HMAC signing key for this project. Stored outside `.agenttalk/` (per-user config dir). The key's existence at the path-derived per-user location automatically activates signature enforcement — there's no config flag to flip. Override the default key path with `AGENTTALK_HMAC_KEY_FILE`. See `SECURITY.md`. |
| `agenttalk reply [--from A] [--kind K] [--subject S] [--meta k=v] -m "body"` | Reply to the most recent received message. Auto-derives recipient (= sender of last message) and echoes `request_id` from the original meta for correlation. Explicit `--meta request_id=...` wins. |
| `agenttalk tail [--from-start] [--interval S] [--timeout S]` | Passive monitor: stream all messages as they arrive. Does **not** advance cursors or write heartbeats — safe to run in a third terminal alongside two active agents. `--from-start` replays existing messages first. |
| `agenttalk serve [--port P] [--host H] [--access-log]` | Start a **read-only** local web dashboard at `http://127.0.0.1:8765/` for browsing the message log in a real browser. **Loopback-only by design** — only `127.0.0.1`, `::1`, and `localhost` are accepted; there is no flag to expose it elsewhere (SSH-tunnel `localhost:<port>` from another machine if needed). HTML output is escaped, strict CSP, `GET`/`HEAD` only, peer-IP check on every method. JSON at `/api/status` and `/api/messages` for scripting. See `SECURITY.md`. |
| `agenttalk install-skills [--claude-only\|--codex-only] [--force] [--dry-run]` | Copy bundled skill files to `~/.claude/commands/` and `~/.codex/skills/`. Idempotent — preserves your local edits unless `--force`. |
| `agenttalk codex-config [--enable\|--disable\|--status]` | Manage per-project sandbox/trust block in `~/.codex/config.toml` so Codex can call agenttalk from inside its sandbox. |
| `agenttalk doctor [--json]` | Health check: store initialized, skills installed + in sync, Codex sandbox block configured, heartbeats fresh. Per the global exit-code contract, exit 2 on any error; warnings exit 0 with the warning state visible in output. |
| `agenttalk status --json` | Structured status output for automation (consult freshness, external tooling). Same data as plain `status` plus `invalid_messages[]` for any messages that fail schema/roster validation. |
| `agenttalk --version` | Print the installed version. |

Message `--kind` values are validated against a fixed vocabulary
(`store.KNOWN_KINDS`); unknown kinds are rejected at write time so a
typo can't produce a "sent" message the receiver will silently skip:

- `message` — generic chat
- `note` — informational
- `question` — needs a reply before the other side proceeds
- `review-request` — "please review this scope"
- `review-result` — "I reviewed; here's my verdict" (use `--meta status=approved|rejected`)
- `wake` — state-change signal for sk-loop (low-latency peer wake)
- `end` — terminate the listen loop on the other side
- `composing` — control-plane: "I'm still drafting a real reply, hold the line." Consumed by `agenttalk wait` as a deadline-extension signal; never returned as a reply. Hidden from `recv` by default. Send via `agenttalk composing` (preferred) or `send --kind composing`.

Adding a new kind requires updating `KNOWN_KINDS` in
`src/agenttalk/store.py` *and* documenting it here + in the skill
bodies. Receivers silently skip messages with unknown kinds (see
`SECURITY.md`).

### Exit codes

Stable across releases — skill bodies and external automation can
rely on these:

| Code | Meaning |
| --- | --- |
| `0` | Success. For `wait`: a message was received. |
| `1` | Reserved for `agenttalk wait` timeout (no new messages within `--timeout`). Loop skills should treat this as "keep waiting", not as an error. |
| `2` | Usage error: missing/invalid identity (`--from`/`--to`/`--for` or `AGENTTALK_SELF`/`AGENTTALK_PEER`), unsafe agent name, identity not in roster, self-mail attempt, malformed `--meta`, corrupt config, missing `.agenttalk/`. Always prints a remediation hint to stderr. |
| `130` | `SIGINT` (Ctrl-C). |

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
