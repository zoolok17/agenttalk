# agenttalk

A small file-backed message bus that lets coding-agent CLIs — **Claude
Code**, **Codex**, or several named instances of either — work on the
same project and message each other directly. Built for the spec-driven
workflow where one agent implements and another reviews, and now able
to model named teams with roles, groups, broadcast fan-out, and an
optional lead role.

Agents share a project-local `.agenttalk/` directory; every message
becomes a small JSON file. Each CLI runs in its own terminal window so
you see the full conversation as it happens. A markdown transcript is
exported on session end.

---

## TL;DR — getting started

```powershell
# one-time install (canonical, tag-pinned)
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.25.0"
agenttalk install-skills          # installs bus skills + the dev-discipline devkit

# in your project root, once per project
agenttalk init --here --agents claude,codex
agenttalk codex-config --enable   # lets Codex call agenttalk from its sandbox
```

Open one terminal per active agent at the project root:

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
  `/agenttalk.consult` (confer with peer before answering you),
  `/agenttalk.propose` (ask the peer to accept/reject/counter a
  concrete solution), `/agenttalk.lead` (coordinate a named team), or
  `/agenttalk.send` (fire and forget).
- **Terminal B (Codex).** `$agenttalk-listen` (wait/respond loop).

On restart or rejoin, confirm identity and inbox state before acting:

```powershell
agenttalk roster
agenttalk status
agenttalk sync --for <agent>
```

`sync` summarizes roster identity, open request threads, recent unread
FYI traffic, terminal decisions, and deterministic next-action hints.
For a known thread, use a scoped wait to wake only on that thread
without advancing the global inbox cursor:

```powershell
agenttalk wait --for <agent> --to-request <request-id>
```

Both terminals show every message as it flies past. When you're done:
`agenttalk end --from claude --reason "done"` writes a markdown
transcript under `.agenttalk/sessions/`.

For a team, initialize with unique names and then add roles/groups:

```powershell
agenttalk init --here --agents claude-dev,codex-dev,claude-rev,codex-rev,claude-lead
agenttalk roster set-role claude-dev implementer
agenttalk roster set-role codex-rev reviewer
agenttalk roster set-group devs claude-dev,codex-dev
agenttalk roster set-group reviewers claude-rev,codex-rev
agenttalk roster --json
```

Use role-suffixed names for clarity. The default `claude,codex` pair
remains valid and needs no roles or groups.

> **Naming convention:** Claude Code uses dotted skill names
> (`agenttalk.send`, `agenttalk.listen`, `agenttalk.handoff`,
> `agenttalk.consult`, `agenttalk.propose`, `agenttalk.lead`,
> `agenttalk.sk-loop`) and
> Codex uses hyphenated names
> (`agenttalk-send`, `agenttalk-listen`, `agenttalk-handoff`,
> `agenttalk-consult`, `agenttalk-propose`, `agenttalk-lead`,
> `agenttalk-sk-loop`).
> Behaviour is identical; only the slash-command spelling differs.

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
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.25.0"
```

Pin to a specific tag so you control upgrades. Replace `v0.25.0` with
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

Install the bundled agent skills once per user:

```powershell
agenttalk install-skills
```

By default this installs two skill families:

- The agenttalk bus skills for cross-agent collaboration:
  `~/.claude/commands/agenttalk.*.md` for Claude Code and
  `~/.codex/skills/agenttalk-*/SKILL.md` for Codex.
- The dev-discipline devkit, shared by both agents:
  `craft-code`, `test-coverage`, `review-code`, `write-docs`, and
  `review-docs` under both `~/.claude/skills/` and `~/.codex/skills/`.

Use `agenttalk install-skills --no-devkit` to install only the bus
skills, or `agenttalk install-skills --devkit-only` to refresh only the
devkit. `--claude-only` and `--codex-only` scope the bus skills only;
the devkit is shared unless you pass `--no-devkit`. Existing edited
files are preserved unless you pass `--force`; use `--dry-run --force`
to preview overwrites first. Restart Claude Code and Codex after
installing or refreshing skills.

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
  state/<agent>.cursor last message id each agent has globally acknowledged
  state/<agent>.threadstate.json per-request seen/closed state for scoped
                       waits (created lazily on first `wait --to-request` /
                       `ack --to-request`, not at init)
  sessions/            markdown/jsonl transcripts written by `agenttalk end`
```

Slash commands and Agent Skills are installed globally (one-time, not
per project) via `agenttalk install-skills` — see the [Install](#install)
section.

---

## Workflows

Open one terminal per active agent at the project root.

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
Terminal A (Claude):   /agenttalk.listen          (passive: wait for messages)
Terminal B (Codex):    $agenttalk-listen          (passive: wait for messages)
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

### First-class proposals — `propose`

Use proposals when you want a named agent to decide on a concrete
solution before either agent proceeds:

```text
Terminal A (Claude):   /agenttalk.propose
Terminal B (Codex):    $agenttalk-propose
```

The CLI command is `agenttalk propose`. It writes `kind=proposal`,
auto-mints `meta.request_id=pp-...` if missing, and prints the
proposal id unless `--quiet`. The proposal body should contain:

```text
## Problem
## Proposed solution
## Alternatives considered
## Tradeoffs
## Decision requested
```

The target responds with:

```powershell
agenttalk reply --kind proposal-response --meta status=accepted -m "..."
```

Use `status=rejected` or `status=countered` instead when appropriate.
A counter closes the old proposal with `status=countered`, then opens a
fresh proposal with `agenttalk propose --in-reply-to <old-request-id>`.
Proposals do **not** bypass the split-work rule: if a proposal assigns
implementation ownership outside spec-kitty, the agents must ask you
first, and every implemented piece still needs a read-only cross-review.

### Teams, groups, and the fresh-review workflow

Use unique names when several agents are active in the same project.
The common team pattern is:

```text
claude-dev   implementation Claude
codex-dev    implementation Codex
claude-rev   fresh-review Claude
codex-rev    fresh-review Codex
claude-lead  optional coordinator
```

Roles are informational labels shown by `agenttalk roster` and
`agenttalk status`; groups are named subsets used by broadcast:

```powershell
agenttalk roster add claude-rev --role reviewer --group reviewers
agenttalk roster set-role codex-dev implementer
agenttalk roster set-group devs claude-dev,codex-dev
agenttalk roster set-group reviewers claude-rev,codex-rev
agenttalk roster
```

The original `claude,codex` pair still works. In a team, each terminal
sets `AGENTTALK_SELF` to its unique name. `AGENTTALK_PEER` is only a
default point-to-point partner; skills ask for or infer an explicit
target when more than one agent could receive a message.

For fresh review, send implementation handoffs to a reviewer that did
not write the code:

```powershell
agenttalk send --from claude-dev --to codex-rev --kind review-request `
  --meta request_id=rq-... --meta base_sha=<sha> --meta head_sha=<sha> `
  -m "<Goal / Files changed / How to verify / Focus areas>"
```

### Broadcast and groups

`agenttalk broadcast` fans out one message per recipient. It does not
create a shared channel and it does not alter per-agent cursors:

```powershell
agenttalk broadcast --from claude-lead --to-group reviewers --kind question `
  --subject "API naming check" `
  -m "Please answer with approve / concern and one sentence of rationale."
```

The command mints a `broadcast_id` like `b-...` and stores it as both
`meta.broadcast_id` and `meta.request_id` on each recipient copy.
Recipients answer the sender with:

```powershell
agenttalk reply --to-request b-... -m "concern: ..."
```

Broadcast `message` and `note` are FYI fan-out. Broadcast `question`
is tracked by `agenttalk threads`: the sender sees responded/pending
recipients, and each recipient sees an owed inbound question until
they reply. There is no special reply-all primitive in this release;
a follow-up to the same audience is a new `agenttalk broadcast`.

### Reply routing and dry-run

`agenttalk reply` resolves an anchor from `--to-id`, `--to-request`,
or the most recent received message for the sender. It replies to the
anchor's sender and echoes the anchor's `request_id` unless you
explicitly set another `request_id`. For broadcast threads, the anchor
sender is the thread originator, not every recipient and not
necessarily the agent who later needs second-hand context.

Use `--dry-run` before sending when several threads are open or when
broadcast routing is easy to misread:

```powershell
agenttalk reply --from codex-rev --to-request b-... --kind message --dry-run
```

Dry-run resolves `--to-id`, `--to-request`, or the last received
message, prints the would-be recipient, request id, and kind, and
sends nothing.

### Lead role

The bundled `/agenttalk.lead` and `$agenttalk-lead` skills describe a
human-facing coordinator role. A lead can decompose work, send
point-to-point assignments, broadcast questions to groups, track
pending responses with `agenttalk threads`, and report the result to
you.

The lead does **not** spawn worker processes. Start each worker in its
own terminal or use a thin external launcher. The lead also does not
replace spec-kitty: inside a spec-kitty mission, `spec-kitty next`
assigns WPs and lanes, while the lead only coordinates around that
state.

A lead is a coordinator, not an authority boundary. A reviewer reports
findings; an implementer changes their owned files; spec-kitty or the
human decides lane state. A "liaison" is only the current contact for
a thread or workstream. After a restart, a lead, reviewer, or liaison
must re-derive state from the repository, the operator, `agenttalk
sync`, `agenttalk threads`, and (inside spec-kitty) `spec-kitty next`;
do not assert stale HOLD/GO decisions from prose in an old message.

### Advisory capacity

For operators coordinating long or parallel runs, `agenttalk capacity`
lets each agent self-publish a coarse local rate-limit snapshot so a
lead can plan work around 5-hour and weekly budget pressure:

```powershell
# run in each agent window to publish that agent's own local signal
agenttalk capacity refresh --for codex

# run from the lead window to view published team snapshots
agenttalk capacity
```

This signal is strictly advisory. Missing, stale, or unknown capacity
must never block protocol progress or decide whether a review is valid.
Use it as a planning hint: steer long work away from a near-cap agent,
prefer short/interruptible tasks when a reset is soon, and tell the
operator when every plausible owner is low or unknown.

Agents publish only normalized metadata under `.agenttalk/state/`:
percent used, reset epochs, window lengths, source, confidence, and
non-secret plan labels. They do not publish raw session files, prompts,
auth paths, token bodies, account ids, or local provider paths.

On Codex, `agenttalk capacity refresh --source codex` reads the local
`~/.codex/sessions/**/rollout-*.jsonl` files, prefers the current
`CODEX_THREAD_ID` when present, and takes the last
`payload.rate_limits` record from the chosen rollout. On Claude Code,
`agenttalk capacity refresh --source claude` reads
`~/.claude/statusline-last-input.json`, which must be kept fresh by a
Claude status line dump. Either enable `CC_STATUSLINE_DEBUG=1` if your
Claude Code build writes that debug input file, or configure a status
line script that writes the latest status-line input JSON to that path.
The JSON needs `rate_limits.five_hour` and `rate_limits.seven_day`
objects with `used_percentage` and `resets_at`.

### Ending the session

```powershell
agenttalk end --from claude --reason "feature shipped"
```

This sends an `end` message to the other agent (breaking its listen
loop) and writes `transcript-<session_id>.md` under
`.agenttalk/sessions/`.

---

## Agent identity, roles, and groups

Agent names are **safe identifiers** — alphanumeric plus dot,
underscore, or hyphen, starting with an alphanumeric, max 64 chars.
This restriction exists because names are interpolated into
`.agenttalk/state/<name>.cursor` (and similar) filenames; anything
that could escape that directory (path separators, `..`, leading
punctuation, quotes, whitespace) is rejected.

The default pair is `claude` and `codex`, but you can run two Claudes,
two Codexes, or a larger team by giving every participant a distinct
name like `claude-dev`, `codex-dev`, `claude-rev`, and `codex-rev`.
Role-suffixed names make transcripts and thread output easier to
scan.

Each terminal declares which agent it is via env vars. In a two-agent
pair, `AGENTTALK_PEER` can name the default recipient. In a larger
team, set `AGENTTALK_SELF` in every terminal and pass explicit
`--to <agent>`, `--to-group <group>`, or `--all` when the recipient is
not obvious:

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

For team metadata, use roster admin commands:

```powershell
agenttalk roster add claude-rev --role reviewer --group reviewers
agenttalk roster add codex-rev --role reviewer --group reviewers
agenttalk roster set-role claude-dev implementer
agenttalk roster set-group devs claude-dev,codex-dev
agenttalk roster --json
```

`all` is an implicit reserved group containing the whole roster. Roles
are informational; groups are used by broadcast fan-out.

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

When your `.agenttalk/` directory is not under the current working
directory, pass `--root` as a **global option before the subcommand**:

```powershell
agenttalk --root D:\Projects\example sync --for claude-dev
```

Do not put it after the subcommand (`agenttalk sync --root ...`);
subcommands do not parse global options there.

`agenttalk init` prints concrete env-setup commands at the end of its
output for 2-agent rosters. For larger teams, use `agenttalk roster`
as the source of truth and set each terminal's `AGENTTALK_SELF`
explicitly.

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

### Windows-safe command bodies

Inline `-m "..."` is fine for short text, but it is fragile for
multi-line bodies, apostrophes, backslashes, and Windows paths. Prefer
`--file <path>` for saved text, or pipe a here-string to `--file -`
for stdin on body-bearing commands such as `send`, `reply`,
`propose`, and `broadcast`:

```powershell
@'
## Goal
Review the changed files.

## Path
D:\Projects\example\src\agenttalk
'@ | agenttalk send --from claude-dev --to codex-rev --kind review-request `
  --meta request_id=rq-docs-001 `
  --meta root=D:\Projects\example `
  --file -
```

For commands where you deliberately use `-m`, put the body in a
here-string variable first:

```powershell
$body = @'
short but path-heavy body: D:\Projects\example\src
'@

agenttalk reply --from codex-rev --to-request rq-docs-001 `
  -m $body
```

Use `--meta key=value` for machine-readable roots, paths, request ids,
and routing data; keep prose bodies for human context. That prevents
backslash/control-character mangling from turning paths into different
strings.

---

## Splitting implementation work between agents

Outside a spec-kitty mission, the skills tell each agent **not to
split implementation work with the peer without first asking you**.
The user invoked them to do a task; the peer is for review or specific
delegated subtasks, not for unilaterally carving up the work.
First-class proposals follow the same rule: a `kind=proposal` can
recommend a concrete plan, but it cannot assign ownership between
agents unless you have approved that split.

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

Two agents listening in parallel roughly double that idle cost; N
agents roughly multiply it by N. Real messages are handled immediately
regardless of timeout (the subprocess returns the instant a message
file lands), so the table is **idle cost only**; actual conversation
rounds add their own per-message cost.

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
| `agenttalk init [--here] [--agents A,B]` | Create `.agenttalk/` in the current dir. Refuses to create a **nested store** when one exists up-tree (0.14.0) — `--force` for a deliberate sandbox. |
| `agenttalk roster [--json]` | Show agents, roles, group memberships, and the resolved current identity. |
| `agenttalk roster add <name> [--role R] [--group G]...` / `remove <name> [--force]` / `set-role <name> <role>` / `set-group <group> <a,b,c>` | Deliberate roster/group admin operations. Groups are validated roster subsets; `all` is implicit and reserved. 0.16.0: `add` refuses a retired-tombstone name; `remove` is refused by default with a retire hint — `--force` removes anyway and warns that history-read breaks (no tombstone — re-addable). |
| `agenttalk roster retire <name> [--reason R]` / `rename <old> <new> [--drain-check] [--reason R]` / `forward <retired> --to <live> --to-request RID [--from A]` | Identity lifecycle (0.16.0, #19). `retire` makes a **permanent tombstone** (can't send, name never re-bound, history stays valid). `rename` = retire `<old>`→tombstone + add `<new>`, carrying over role/group/operator-facing; `--drain-check` refuses while work is owed to/from `<old>`. `forward` redirects a single owed request to a live agent, transcript-visible. |
| `agenttalk barrier bump --from A --scope global [-m REASON] [--json]` | Fire a **global epoch barrier** (0.16.0, #19): one meta-marked message whose id becomes the new epoch, marking everything before it as a previous epoch. Any active member may bump (trusted-team global-stall lever). Tracked openers after it record `epoch_at_send` automatically. |
| `agenttalk whoami [--for A] [--json]` | Show effective root, resolved self and peer, roster membership, role/groups, unread count, and owed-thread count. Warns when identity is unresolved or not in the roster, which is often a wrong `--root` or env issue. |
| `agenttalk status` | Show roster, per-agent cursor, unread count, and **actionable warnings**: never-acked unread, soft-deadlocks, unconsumed correlated replies, and stale outbound threads. |
| `agenttalk threads [--for A] [--all] [--json]` | Derive request/reply thread state from validated messages. Default view shows actionable rows only (`reply-waiting`, `owed-inbound`, `open-outbound`); `--all` includes `closed`. 0.16.0: open rows in `--json` carry read-only `next_owner` / `next_action` (`reply`/`read-reply`/`await-reply`/`answer-operator`) — who owes the next move, a pure projection of state. |
| `agenttalk sync --for A [--json]` | Rejoin digest: show identity, roster, actionable threads grouped by request id, terminal decisions, recent unread non-action messages, and deterministic next-action hints. Pure derivation; no cursor or threadstate writes. |
| `agenttalk capacity [show\|refresh] [--for A] [--source auto\|claude\|codex] [--threshold N] [--reset-soon-min N] [--statusline-path PATH] [--sessions-dir PATH]` | Advisory rate-limit budget snapshots. `refresh` reads the caller's local Claude/Codex signal and publishes a normalized snapshot for `A`; `show` (the default) prints the team's published 5-hour/weekly usage, stale/unknown confidence, and near-cap/reset-soon flags. Never gates progress. |
| `agenttalk send --from A --to B [--kind K] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -)` | Drop a message into the bus. `--file -` reads the body from stdin. `review-request`, `question`, and `proposal` without `--meta request_id=...` get one minted + printed; `wake` gets a `wk-` correlation id minted the same way but does **not** open a thread (0.24.0); `review-result` and `proposal-response` without one warn (soft, exit 0). |
| `agenttalk broadcast --from A (--to-group G \| --all) [--kind message\|note\|question] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -) [--print-id] [--quiet]` | Fan out one message per recipient, excluding the sender. Mints `broadcast_id=b-...`, stores it as `meta.broadcast_id` and `meta.request_id`, and prints the recipient list unless quiet. 0.15.0: `--to-role <role>` targets every member holding a role (frozen into each copy at send time); a PARTIAL fan-out exits **5** with a delivered/missed manifest — recover with `--resume <bid>` or rescind. |
| `agenttalk propose [--from A] [--to B] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -) [--in-reply-to ID] [--print-id] [--quiet]` | Send a first-class `proposal`. Auto-mints `meta.request_id=pp-...` if absent and prints `(proposal id: pp-...)` unless quiet. `--in-reply-to` sets `meta.in_reply_to` for counters. |
| `agenttalk recv --for A [--ack] [--since ID] [--include-control]` | **Peek** at queued messages — does NOT move the cursor unless `--ack`. Plain `recv` that prints messages emits a hint pointing at `drain`. Hides `composing` pings by default; `--include-control` surfaces them. |
| `agenttalk drain --for A [--include-control]` | **Consume**: print all unread AND advance the cursor to newest, in one shot. Same path as `recv --ack`. Use this instead of hand-rolled timestamp polling. |
| `agenttalk wait --for A [--to-request RID] [--kind K] [--timeout 120] [--no-ack] [--grace 2] [--composing-extend 120]` | Plain wait blocks until a new real message arrives, prints it, and advances the global cursor unless `--no-ack`. Scoped wait (`--to-request` and/or `--kind`) returns only matching addressed messages, advances only the per-thread `seen_msg_id`, and never advances the global cursor. A scoped wait on a rescinded request wakes immediately with **exit 3** (0.14.0). |
| `agenttalk composing --from A [--to-request RID] [-m "still drafting"]` | Send a `composing` ping so the peer's `wait` extends its deadline. Use periodically while you draft a long reply. The peer's `wait` consumes these as deadline-extension signals — they do NOT surface as a returned reply. With `--to-request` (0.14.0) the peer is derived from the thread, and a **reply-in-flight** marker shows up in their `threads`/`sync`. |
| `agenttalk ack --for A [--id ID] [--to-request RID]` | Without `--to-request`, manually move an agent's global cursor forward. With `--to-request`, manually close that request thread for A and record the latest seen matching message without touching the global cursor. |
| `agenttalk rescind --from A --to-request RID [--to-id MSG] [-m REASON]` | Mark a tracked request you opened as **no-longer-current** (0.14.0). Transcript-visible; the thread becomes `closed-superseded`, a peer blocked in `wait --to-request` wakes with exit 3, and `check` reports superseded. Requester-only. Prefer this over a prose "ignore my last message". |
| `agenttalk check --for A --to-request RID [--epoch] [--json]` | **Pre-action currentness gate** (0.14.0): prints `current`/`superseded`/`unknown`, exits 0/3/4. Run it immediately before any irreversible action tied to a request — exit 3 is a hard stop. Read-only; a local `ack` never masks a rescind. 0.16.0 `--epoch` also checks the global epoch: exit **3** if the request predates the latest barrier (previous-epoch, or a pre-epoch opener to re-ask). Adds an additive `epoch` object to `--json`. |
| `agenttalk escalate --from A (-m TEXT \| --file -) [--to X]` | Route an operator question to the **liaison**, falling back to the single `role=lead` agent when no liaison is configured (0.24.0). Resolution: `--to` → liaison → sole lead → refuse. Mints an `esc-` request_id (printed as `request_id=<id>`); refuses (exit 2) only when none of those resolve, with a remediation naming both `set-operator-facing` and `set-role … lead`. |
| `agenttalk roster set-operator-facing <name>` / `--clear` | Designate the ONE agent the human operator talks to directly (0.14.0). Advisory routing metadata, single slot — "two liaisons" is unrepresentable. |
| `agenttalk prune --invalid [--dry-run] [--json]` | Quarantine invalid message files into `.agenttalk/quarantine/` (0.15.0) — move-only and **recoverable** (restore = move the file back); the selection is exactly what status reports as INVALID; valid files untouched by construction. |
| `agenttalk transcript [--format md\|jsonl] [--out PATH]` | Export the full conversation. |
| `agenttalk end --from A [--reason ...]` | Notify the other agent(s) and write the transcript. In a team, sends `end` to every other roster member. |
| `agenttalk reset [--archive]` | Clear **active bus state** (messages + cursors + heartbeats); preserves historical transcripts under `.agenttalk/sessions/` so past exports aren't lost. Bumps `session_id`. With `--archive`, instead moves **everything** (messages + state + sessions) under `.agenttalk/archived/<old_session>/`. Preserves config (roster) either way. |
| `agenttalk hmac-init [--force]` | Generate the HMAC signing key for this project. Stored outside `.agenttalk/` (per-user config dir). The key's existence at the path-derived per-user location automatically activates signature enforcement — there's no config flag to flip. Override the default key path with `AGENTTALK_HMAC_KEY_FILE`. See `SECURITY.md`. |
| `agenttalk reply [--from A] [--to-id MSG_ID \| --to-request REQUEST_ID] [--kind K] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -) [--dry-run]` | Reply to the most recent received message, or anchor to a specific received message/thread. Auto-derives recipient and echoes the anchor's `request_id`; explicit `--meta request_id=...` wins. `--dry-run` prints the resolved recipient, request id, and kind without sending. A reply that opens a new thread (`review-request` or `proposal`) mints a fresh id instead of echoing. 0.15.0: `--na` sends a not-applicable response — closes your obligation, displayed as (n/a); refused on review-request/proposal threads. |
| `agenttalk tail [--from-start] [--interval S] [--timeout S]` | Passive monitor: stream all messages as they arrive. Does **not** advance cursors or write heartbeats — safe to run in a third terminal alongside two active agents. `--from-start` replays existing messages first. |
| `agenttalk serve [--port P] [--host H] [--access-log]` | Start a **read-only** local web dashboard at `http://127.0.0.1:8765/` for browsing the message log in a real browser. **Loopback-only by design** — only `127.0.0.1`, `::1`, and `localhost` are accepted; there is no flag to expose it elsewhere (SSH-tunnel `localhost:<port>` from another machine if needed). HTML output is escaped, strict CSP, `GET`/`HEAD` only, peer-IP check on every method. JSON at `/api/status` and `/api/messages` for scripting. 0.17.0: the same server also serves `/dashboard` (the obligation view) and `/api/state`; a port that can't be bound now exits **2** with a `--port 0` hint instead of a raw traceback. See `SECURITY.md`. |
| `agenttalk dashboard [--port P] [--store PATH]... [--access-log]` | The **obligation dashboard** (0.17.0): who owes what, whose turn it is, and the next action — per agent, per open thread, with mission/WP tags and epoch staleness. Same read-only loopback-only server as `serve`, landing on `http://127.0.0.1:8765/dashboard`; auto-refreshes every ~2 s. Repeat `--store <project-root>` to watch **several projects in one tab** (each path is the project root itself — no upward search; an uninitialized path shows as a degraded panel, not an error). No `--host` option exists on this spelling. `GET /api/state` (`schema_version: 1`) is the same data for scripting. |
| `agenttalk install-skills [--claude-only\|--codex-only] [--no-devkit\|--devkit-only] [--force] [--dry-run]` | Copy bundled bus skills to `~/.claude/commands/` and `~/.codex/skills/`, and by default copy the shared dev-discipline devkit (`craft-code`, `test-coverage`, `review-code`, `write-docs`, `review-docs`) to both `~/.claude/skills/` and `~/.codex/skills/`. `--claude-only` and `--codex-only` scope only the bus skills; use `--no-devkit` to skip the shared devkit. Idempotent — preserves your local edits unless `--force`; use `--dry-run --force` to preview overwrites. |
| `agenttalk codex-config [--enable\|--disable\|--status]` | Manage per-project sandbox/trust block in `~/.codex/config.toml` so Codex can call agenttalk from inside its sandbox. |
| `agenttalk doctor [--json]` | Health check: store initialized, bus skills installed + in sync, devkit absent/in sync/stale state surfaced, Codex sandbox block configured, heartbeats fresh. Per the global exit-code contract, exit 2 on any error; warnings exit 0 with the warning state visible in output. |
| `agenttalk status --json` | Structured status output for automation (consult freshness, external tooling). Same data as plain `status` plus `invalid_messages[]`, `warnings[]`, per-agent `waiting` / `waiting_stale`, and thread-derived warning state (additive — existing keys unchanged). |
| `agenttalk --version` | Print the installed version. |

### Rejoining a session with `sync`

Use `agenttalk sync --for A` before an agent acts after a restart,
context compaction, or long idle period. It is read-only: it derives a
digest from roster data, validated messages, global cursor state, and
per-thread `threadstate.json`, but does not acknowledge anything.

The digest separates actionable thread work from recent FYI traffic.
It groups request/reply threads by `request_id`, shows whether `A`
owes action or is waiting on someone else, includes recent terminal
results such as `review-result` and `proposal-response`, and prints
deterministic command hints such as `reply --to-request`,
`wait --to-request`, `ack --to-request`, or `drain` when the next
step is mechanically knowable. Use `--json` for automation.

### Reading the inbox: peek vs consume

A single rule prevents the cursor footgun that caused a two-agent
deadlock in practice (issue #5):

- **`recv` peeks.** It prints what's queued but does **not** move your
  cursor, so the same messages re-print every call and `unread` climbs.
  Good for a quick look; never build polling on top of it.
- **`drain` (or `recv --ack`) consumes.** It prints unread *and*
  advances the cursor to newest. This is the "I've read these, don't
  show them again" operation.
- **`wait` consumes one real message.** It blocks, returns the next
  real message, and advances the cursor past it (`--no-ack` to opt out).
- **Scoped `wait` is thread-local.** `wait --to-request <id>` and
  `wait --kind <kind>` return only matching addressed messages. They
  advance only the per-thread `seen_msg_id` pointer so the same scoped
  wait can progress, but they do not advance the global cursor or
  mark the thread handled.
- **`--since ID` inspects history** without touching the cursor.
- **`ack --to-request <id>` closes a handled thread manually.** It is
  the escape hatch for off-contract replies or already-handled work
  and does not advance the global cursor.

If you ever find yourself comparing message timestamps to a baseline to
detect "new" activity, stop — that's the footgun. Use `drain` / the
cursor instead. `agenttalk status` will warn if an agent has unread
with a never-set cursor, and flag a soft-deadlock if both agents are
blocked in `wait` at the same time.

### Tracking request/reply threads

`agenttalk threads --for A` derives open request/reply state from
validated messages only. It tracks messages that carry
`meta.request_id`.

Openers:
- `review-request`
- `question`
- `proposal`

Expected responses:
- `review-request` -> `review-result`
- `proposal` -> `proposal-response`
- `question` -> any non-control response from the expected
  counterparty with the same `request_id`

Broadcast questions use the same `request_id` for every fan-out copy
and also carry `meta.broadcast_id` plus `meta.audience`. From the
broadcaster's perspective, the thread stays open until every recipient
has responded and those responses have been consumed. From a
recipient's perspective, their copy is an owed inbound question until
they answer it with `agenttalk reply --to-request <b-id> ...`.

`agenttalk ack --for A --to-request <request_id>` is a manual closure
override for A's view of a handled thread. It records the latest seen
matching message in `.agenttalk/state/<agent>.threadstate.json` and
marks the thread closed without advancing A's global cursor. This is
useful when a response was semantically handled but did not match the
strict review/proposal response kind, or when an agent has already
handled a thread after a restart.

Thread states from `--for A`'s perspective:
- `reply-waiting`: a correlated response addressed to `A` is newer
  than `A`'s global cursor; consume it with `drain`, plain `wait`,
  or targeted `wait --to-request <id>`.
- `owed-inbound`: the ball is on `A`; either the peer's opener has no
  response from `A`, or a consumed `review-result status=needs-info`
  bounced the ball back.
- `open-outbound`: the ball is on the peer; `A`'s opener has no
  response, or `A` sent `needs-info` and is awaiting the peer's info.
- Broadcast `open-outbound`: the broadcaster is waiting on one or
  more pending recipients. Output shows responded/pending counts and
  names.
- `closed`: a terminal correlated response exists (`approved`,
  `rejected`, `accepted`, any non-control question answer, or
  `proposal-response status=countered` for that proposal thread).

Default output shows only actionable rows. `--all` includes closed
threads. `--json` emits:

```json
{
  "agent": "<me>",
  "threads": [
    {
      "request_id": "rq-...",
      "opener_kind": "review-request",
      "subject": "WP ready",
      "peer": "claude",
      "role": "opener",
      "state": "open-outbound",
      "age_seconds": 42,
      "last_msg_id": "20260602-...",
      "unread": false
    }
  ],
  "counts": {
    "reply-waiting": 0,
    "owed-inbound": 0,
    "open-outbound": 1,
    "closed": 0
  }
}
```

Message `--kind` values are validated against a fixed vocabulary
(`store.KNOWN_KINDS`); unknown kinds are rejected at write time so a
typo can't produce a "sent" message the receiver will silently skip:

- `message` — generic chat
- `note` — informational
- `question` — needs a reply before the other side proceeds
- `review-request` — "please review this scope"
- `review-result` — "I reviewed; here's my verdict" (use `--meta status=approved|rejected|needs-info`)
- `proposal` — "I propose this concrete solution; accept, reject, or counter"
- `proposal-response` — verdict on a proposal (use `--meta status=accepted|rejected|countered`)
- `wake` — state-change signal for sk-loop (low-latency peer wake)
- `end` — terminate the listen loop on the other side
- `composing` — control-plane: "I'm still drafting a real reply, hold the line." Consumed by `agenttalk wait` as a deadline-extension signal; never returned as a reply. Hidden from `recv` by default. Send via `agenttalk composing` (preferred) or `send --kind composing`.

Adding a new kind requires updating `KNOWN_KINDS` in
`src/agenttalk/store.py` *and* documenting it here + in the skill
bodies. Receivers silently skip messages with unknown kinds (see
`SECURITY.md`).

### Rescinding a request — supersession and the `check` gate (0.14.0)

A tracked request can become wrong after it is sent (new data, a HOLD,
a changed plan). Prose cannot fix that: the bus has no idea your "ignore
my last message" relates to the earlier thread, a blocked `wait` will not
wake for it, and an executor that already read the request will still act.

`agenttalk rescind --from A --to-request RID -m "<why>"` is the
first-class cancel: requester-only, transcript-visible, and correlated.
Derivation flips the thread to `closed-superseded` for every participant
(the FIRST qualifying rescind decides; later duplicates are audit-only),
a peer blocked in `wait --to-request RID` wakes immediately with a
`RESCINDED` banner and **exit 3**, and `sync` flags rescinded threads the
agent has not yet consumed. A re-ask after a rescind needs a fresh
request_id — same contract as manual `ack` closure. A manual `ack` keeps
its own `closed` label (you said you handled it), but it never masks the
fact: `check` answers from the validated log alone.

The race no inbox primitive can close: the executor drained the request
minutes ago and is about to act — no waiting, no reading. That is what
`agenttalk check --for A --to-request RID` is for: run it **immediately
before any irreversible action** tied to a request. Exit 0 = current,
act. Exit 3 = superseded — hard stop (the output names who rescinded,
when, and why). Exit 4 = unknown id — treat as stale. The bundled skills
encode this contract; it is the operator-safety barrier from the
production HOLD/fire incident.

### The operator liaison — one voice to the human (0.14.0)

In a team where one human operates several agent windows, designate ONE
agent as the operator channel: `agenttalk roster set-operator-facing
<name>`. Workers that need a human decision then run `agenttalk escalate
--from W -m "<decision, options, recommendation>"` instead of asking the
human at their own window. The escalation is an ordinary tracked question
(meta `needs_operator=true`, `esc-` request_id, printed as
`request_id=<id>` for the follow-up `wait --to-request`); it is routed to
the liaison automatically and refuses loudly (exit 2) when no liaison is
configured — an escalation that lands nowhere is exactly the silent
failure this kills. The liaison's `sync` shows pending escalations under
**OPERATOR INPUT NEEDED**; answering on the same request_id (optionally
`--meta operator_answer=true`) clears them.

Honest scope: this is **advisory routing metadata**, not enforcement —
the bus cannot control what a human types into which window (see
SECURITY.md). `doctor` warns when the designation is missing-but-needed,
stale, or points at a pruned agent.

### Root resolution and `AGENTTALK_ROOT` (0.14.0)

The bus root resolves with strict precedence: **`--root` flag >
`AGENTTALK_ROOT` env var > upward walk from CWD** to the first
`.agenttalk/`. A pinned root (flag or env) that has no store fails
loudly — it never falls back to the walk, so a typo cannot silently
route a window to a different store. `init` refuses to create a nested
store when one already exists up-tree (the production split-brain was
exactly two `init`s at different depths); `doctor` names every store on
the path and leads with `root:` — as does `whoami`. Pin the root per
shell with `$env:AGENTTALK_ROOT = '<project root>'` and every window
agrees by construction.

### Role audiences and honest n/a replies (0.15.0)

`agenttalk broadcast --to-role reviewer --kind question -m "fresh eyes?"`
fans out to every roster member whose ROLE is `reviewer` — no
hand-curated group needed. The audience is **frozen into each copy at
send time** (`audience_kind`/`audience_resolved`/`batch_total` meta):
change the roles map afterwards and historical obligations do not move,
because thread derivation only ever reads the copies themselves.

When a broadcast question genuinely does not concern you,
`agenttalk reply --to-request <bid> --na` closes your obligation with a
structured not-applicable response. The asker sees `na=[you]` instead of
mistaking it for a substantive answer — and nobody placeholder-acks or
goes silent. NA is refused on review-request/proposal threads (those
contracts need `review-result` / `proposal-response`).

### Broadcast delivery accounting (0.15.0)

Fan-out has no multi-file atomicity on a local filesystem, so agenttalk
is honest instead: every copy carries the batch facts, and a mid-batch
failure prints `delivered=[...]` / `missed=[...]` and exits **5**.
Recovery is one command — `agenttalk broadcast --resume <bid>` re-sends
the missing copies from the frozen originals (broadcaster-only) — or
rescind the thread to void it. Until resolved, `status` warns
`incomplete fan-out` naming the missed members.

### Quarantine — recoverable store hygiene (0.15.0)

`agenttalk prune --invalid` moves every file the INVALID report names
into `.agenttalk/quarantine/`. Move-only: never overwritten (collisions
get a timestamp suffix), never deleted by the tool, restore = move the
file back into `messages/`. Selection is the SAME validation gate walk
status/doctor use, path-paired at scan time, so a valid file can never
be selected. Use `--dry-run` to inspect first; `doctor` shows
invalid/quarantined counts.

### The obligation dashboard (0.17.0)

spec-kitty's kanban shows the *task* layer; `agenttalk dashboard` shows
the *conversation/obligation* layer of the bus itself — and works
without spec-kitty. One browser tab answers: which agents are alive
(heartbeat age), who has unread backlog, who is composing, which
threads are open, **whose court the ball is in** (`next_owner`) and
what the move is (`next_action`), plus mission/WP tags when messages
carry them in meta and epoch staleness after a `barrier bump`.

```powershell
# one project (current root):
agenttalk dashboard

# two live sessions, one tab:
agenttalk dashboard --store D:\proj\band-a --store D:\proj\band-b
```

For automation, `GET /api/state` returns the same aggregate as
versioned JSON (`schema_version: 1`): an array of root objects, each
fully namespaced (no cross-root merging), with per-root `errors` as
data — one corrupt store renders as a degraded panel while the others
stay live. Thread rows carry subjects and derived fields only, never
message bodies; body detail stays on the existing `/messages/<id>`
routes (first root only). If a watched project contains `kitty-specs/`,
the panel lists its missions — detection is filesystem-only, agenttalk
never imports spec-kitty.

The loopback story is unchanged and non-negotiable: no auth, no
remote-bind flag on any spelling — SSH-tunnel the port if you need it
from another machine.

**0.19.0 polish.** The `/dashboard` view now renders a **hierarchical
team layout** — the operator-facing liaison (or a lead-ish role) on top,
developer-ish roles grouped left, reviewer-ish right (classified from the
roster's `role`/`operator_facing`, client-side) — with an **agent card**
per member showing last-seen, **messages sent/received**, how many
threads it **owes**, and a composing badge. Below the roster, a
**who-talks-to-whom** conversation panel lists message traffic as
directed `from → to (count)` pairs. A **Refresh** button pulls fresh
state on demand and an **auto-refresh toggle** (on by default) turns the
~2 s polling on/off without reloading the page. For automation,
`/api/state` gains additive keys (`schema_version` stays `1`): per-agent
`sent`/`received` and a per-root `edges` array (`{from,to,count}`, top
50, with `edges_truncated`/`edge_limit` when capped). Stats are
**bus-native only** — agenttalk never imports spec-kitty or reads token
usage; it shows what the message store actually contains.

### One window per agent (and the clock-agreement caveat)

Two operating assumptions are worth stating plainly (0.18.0):

- **One window per agent.** Each agent is meant to run in exactly one
  window per store. Same-agent concurrent consumers are **unsupported**:
  `advance_cursor` / `mark_thread_seen` / `close_thread` are atomic
  *writes* but not process-safe read-modify-write, so two windows draining
  the same agent can lose cursor/threadstate updates. 0.18.0 *warns* when
  `agenttalk wait` detects another live process already waiting as the same
  agent (advisory, best-effort — it never blocks and never changes the exit
  code), and `agenttalk doctor` reports the current waiter's PID. It does
  **not** enforce single-writer locking — the warning is a guardrail, not a
  guarantee.
- **Synced stores assume clock agreement.** Message ids are
  timestamp-prefixed and delivery order is a lexical compare of ids. If you
  sync one `.agenttalk/` across machines whose clocks disagree, a
  future-dated id from the fast machine can mis-order or hide later messages
  from the slow one. 0.18.0 rejects malformed (wrong-shape) ids, but a
  well-formed *future-dated* id from clock skew is not caught — keep the
  machines' clocks in agreement (e.g. NTP).

### Exit codes

Stable across releases — skill bodies and external automation can
rely on these:

| Code | Meaning |
| --- | --- |
| `0` | Success. For `wait`: a message was received. |
| `1` | Reserved for `agenttalk wait` timeout (no new messages within `--timeout`). Loop skills should treat this as "keep waiting", not as an error. |
| `2` | Usage error: missing/invalid identity (`--from`/`--to`/`--for` or `AGENTTALK_SELF`/`AGENTTALK_PEER`), unsafe agent name, identity not in roster, self-mail attempt, malformed `--meta`, corrupt config, missing `.agenttalk/`. 0.17.0: also a `serve`/`dashboard` bind failure (port in use / OS-denied) — with a `--port 0` remediation hint. Always prints a remediation hint to stderr. |
| `5` | Partial broadcast fan-out: some copies written, some failed (see the delivered/missed manifest; `--resume`). 0.18.0: a frozen recipient retired *after* a partial fan-out is reported under `dropped` and skipped — it no longer traps `--resume` at a permanent exit 5; an all-retired remainder resolves to exit 0. |
| `130` | `SIGINT` (Ctrl-C). |

---


0.14.0 additions: **3** = the request was superseded/rescinded
(`check`, and a scoped `wait --to-request` waking on a rescind);
**4** = unknown request id (`check`); **5** = PARTIAL broadcast fan-out (0.15.0 — some copies written, some failed; see the delivered/missed manifest). Exit 1 remains *exclusively* the
`wait` timeout; 2 remains usage/refusal.

## How terminals see messages

Each `send` writes the message file **and** prints the rendered message
to the sender's stdout. The receiver's `wait` (running in another
terminal) picks up the same file and prints it on that side. So both
terminals show both halves of the exchange - and the full conversation
is on disk in `.agenttalk/messages/` as the source of truth.

Broadcast is fan-out, not a shared channel. `agenttalk broadcast`
writes one ordinary message per recipient, all with the same
`broadcast_id` / `request_id`. Broadcast does not alter per-agent
cursors; recipients read it through the same global or scoped wait
paths as any other addressed message.

The transcript exporter walks `messages/` in id order (which is
chronological) and renders it as markdown.

---

## Design notes

- **No daemon.** Just files. Survives reboots, terminal crashes, agent
  restarts.
- **No append contention.** One JSON file per message, written atomically
  via temp-file + `os.replace` (atomic on NTFS and POSIX).
- **Global cursor plus per-thread state.** Plain inbox reading lists
  messages newer than the agent's global cursor; global ack moves that
  cursor. Scoped waits use additive per-thread `seen_msg_id` /
  `closed` state so an agent can work one request without consuming
  unrelated inbox traffic.
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
