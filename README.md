# agenttalk

A small, file-backed bus that lets coding-agent CLIs — **Claude Code**
and **Codex**, a pair or a named team — **talk to each other directly**
and work on the same repo. No daemon, just files.

At its core it's still exactly that: two agents messaging each other so an
implementer and a reviewer collaborate without you copy-pasting between
windows. Around that core it has grown to meet real multi-agent work —
named teams (roles, groups, broadcast, a lead/liaison), operator-safety
primitives (escalation, supersede/rescind, pre-action checks, epochs),
24/7 unattended supervision that auto-restarts agents *with their context
intact*, a shared ownership/domain registry, and a lightweight
review-assurance layer (gates + typed evidence). **The essence is
unchanged; the surface area grew.**

Agents share a project-local `.agenttalk/` directory; every message
becomes a small JSON file. Each CLI runs in its own terminal window so
you see the full conversation as it happens. A markdown transcript is
exported on session end.

## Capabilities at a glance

| Layer | What it gives you |
| --- | --- |
| **Talk directly** | `send`/`reply` point-to-point or `broadcast` fan-out; every message is a JSON file both terminals see. |
| **Review handoffs** | the `/agenttalk.handoff` and `/agenttalk.consult` skills (`$agenttalk-…` on Codex) plus the `agenttalk propose` command — fresh cross-review by an agent that didn't write the code. |
| **Named teams** | roles, groups, a `lead`/operator-liaison; `escalate` routes decisions to one human voice. |
| **Operator-safety** | supersede/`rescind`, pre-action `check`, epoch barriers — stale or rescinded requests can't quietly close. |
| **24/7 supervision** | auto-restart agents *with their session intact* across outages; a progress wrapper (`wrap`) for visibility. |
| **Shared ownership** | a `domain` registry mapping repo areas to owners/reviewers. |
| **Assurance** | `gate` HOLD/GO state + typed review evidence, so unsafe closure is hard. |

The first row is the whole essence; everything else is opt-in.

## Documentation

Start with [docs/AGENTTALK-NEW-USER-MANUAL.md](docs/AGENTTALK-NEW-USER-MANUAL.md)
for a concept-first onboarding manual. A generated PDF is available at
[docs/AGENTTALK-NEW-USER-MANUAL.pdf](docs/AGENTTALK-NEW-USER-MANUAL.pdf).
For operator-facing procedures and examples, use
[docs/USER-MANUAL.md](docs/USER-MANUAL.md). The other canonical docs are:

- [docs/AGENT-MANUAL.md](docs/AGENT-MANUAL.md) - role-keyed operating guide
  for agents.
- [docs/DESIGN.md](docs/DESIGN.md) - architecture, rationale, and decision
  history.
- [docs/ROADMAP.md](docs/ROADMAP.md) - product roadmap.
- [docs/ASSURANCE.md](docs/ASSURANCE.md) - release attestation and gate
  evidence.
- [docs/ISSUES.md](docs/ISSUES.md) - living tracker.
- [CHANGELOG.md](CHANGELOG.md) - release history.
- [SECURITY.md](SECURITY.md) - security posture and trust model.

---

## The minimal start (just tell the agent)

You don't have to pre-declare a roster or learn any commands to get an agent
onto the bus. If a project already has agenttalk initialized and the skills
installed (the one-time setup in the [TL;DR](#tldr--getting-started) below),
just start a fresh CLI and tell the agent, in plain language:

> We use agenttalk in this project. Give yourself a unique name, add yourself
> to the roster as a developer (or reviewer), then wait for the lead to contact
> you.

The agent reads its agenttalk skill, picks a name, runs `agenttalk roster add
<name> --role developer`, and drops into listen mode (`/agenttalk.listen`).
That's the whole entry gate — the lead (or you) drives it from there. **Roles
are free-form labels**, so `developer`, `reviewer`, `tester`, or anything else
you name works.

**Starting the human-facing lead** is the same move from the other side — tell
one CLI:

> We use agenttalk in this project. Add yourself as the human-facing lead, then
> check whether the team is up and running.

It picks a name, runs `agenttalk roster add <name> --role lead` and `agenttalk
roster set-operator-facing <name>` (so it's the single agent you talk to and
where escalations route), reads its `/agenttalk.lead` skill, then runs `agenttalk
roster` / `status` / `sync` to see who's online — and coordinates the team from
there.

The precise setup below — `init --agents ...` and explicit roster/role/group
commands — is for when you want names and structure pinned up front. It is *not*
a prerequisite.

---

## TL;DR — getting started

```powershell
# one-time install (canonical, tag-pinned)
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.73.1"
agenttalk install-skills          # installs bus skills + the dev-discipline devkit

# in your project root, once per project
agenttalk init --here --agents claude,codex
agenttalk codex-config --enable   # lets Codex call agenttalk from its sandbox
```

A one-window project is valid too: initialize with `--agents claude` or
`--agents codex`, set only `AGENTTALK_SELF`, and add peers later. Until then,
commands that need another agent require an explicit `--to` and cannot infer a
peer.

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
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.73.1"
```

Pin to a specific tag so you control upgrades. Replace the tag with
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
- The **assurance review/test pack** (also part of the devkit):
  `review-failure-injection`, `review-contract-drift`,
  `review-release-readiness`, `system-review-protocol`, and `tester-qa`.
  These are generic review/tester skills that PRODUCE the typed
  `review-result` evidence + `risk_class` the assurance close consumes
  (see "Milestone/release close" and "Specialist sign-off by risk
  class"), so a reviewer or tester can sign off through `agenttalk close`.
  agenttalk ships the generic skeletons + the evidence/honesty rules; the
  PROJECT supplies the domain checklists, `.agenttalk/signoffs.json` risk
  policy, and CI gates (e.g. Android a11y/device/GL stays project
  content). Two rules are baked into every skill: `tests_executed` is what
  you actually RAN (real command + result, or a CI run id), never a
  claim — release-blocking evidence anchors to an `automation_ci` gate;
  and a skill proposes a `risk_class` but never decides the close's risk
  (the lead-owned risk inventory is authoritative for routing).

Use `agenttalk install-skills --no-devkit` to install only the bus
skills, or `agenttalk install-skills --devkit-only` to refresh only the
devkit. `--claude-only` and `--codex-only` scope the bus skills only;
the devkit is shared unless you pass `--no-devkit`. Existing edited
files are preserved unless you pass `--force`; use `--dry-run --force`
to preview overwrites first. Restart Claude Code and Codex after
installing or refreshing skills.

**Upgrading to v0.43.0+ (skill frontmatter migration):** the bundled
skills now carry currency frontmatter (`reviewed-against`, plus
`category` / `evidence-profile` on devkit skills). After upgrading,
`agenttalk doctor` will show the devkit as **stale** (your installed copies
predate the migration). Refresh with `agenttalk install-skills
--devkit-only --force` to adopt the new bundled skills; preview first with
`agenttalk install-skills --devkit-only --dry-run --force` if you have local
skill edits you want to keep.

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

The roster must contain at least one agent. For a single-agent start, use
`agenttalk init --here --agents claude` or `--agents codex`; set only
`AGENTTALK_SELF` until another roster member is added.

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
lets each agent self-publish a coarse local headroom snapshot so a
lead can plan work around both 5-hour/weekly rate-limit pressure and
context-window compaction risk:

```powershell
# run in each agent window to publish that agent's own local signal
agenttalk capacity refresh --for codex

# run from the lead window to view published team snapshots
agenttalk capacity
```

This signal is strictly advisory. Missing, stale, or unknown capacity
must never block protocol progress or decide whether a review is valid.
Use it as a planning hint: steer long work away from a near-cap agent,
prefer short/interruptible tasks when a reset is soon, avoid assigning
large context-heavy work to an agent near compaction, and tell the
operator when every plausible owner is low, near compaction, stale, or
unknown.

Agents publish only normalized metadata under `.agenttalk/state/`:
rate-limit percent used, reset epochs, budget window lengths,
context-window percent used, context window size/current context tokens,
source, confidence, and non-secret plan labels. They do not publish raw
session files, prompts, auth paths, token bodies, account ids, or local
provider paths.

On Codex, `agenttalk capacity refresh --source codex` reads the local
`~/.codex/sessions/**/rollout-*.jsonl` files, prefers the current
`CODEX_THREAD_ID` when present, and takes the last record carrying
`payload.rate_limits` and/or context data in `payload.info`. Codex
context fill uses
`info.last_token_usage.input_tokens / info.model_context_window * 100`;
it does not use cumulative `total_token_usage`.

On Claude Code, `agenttalk capacity refresh --source claude` reads
`~/.claude/statusline-last-input.json`, which must be kept fresh by a
Claude status line dump. Either enable `CC_STATUSLINE_DEBUG=1` if your
Claude Code build writes that debug input file, or configure a status
line script that writes the latest status-line input JSON to that path.
The JSON may carry rate-limit data in `rate_limits.five_hour` and
`rate_limits.seven_day`, context data in `context_window`, or both.
Context data uses `context_window.used_percentage`,
`context_window.context_window_size`, and input-side
`context_window.current_usage` token counts.

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

## Domains: a shared ownership registry (experimental, 0.31.0)

A **domain** is a named slice of the repo with owners, reviewers,
curators, and a set of owned globs — declared once in a project-local
registry at `.agenttalk/domains.json`. It is the shared spine that
upcoming **lane** (diff-bounds / deliver gates) and **knowledge**
features will hang off, so they reference one ownership model instead of
inventing their own.

0.31.0 ships the **foundation**: the registry plus read-only inspection
and validation. There is no mutation command yet — you author
`domains.json` by hand. The **lane** deliver-gate builds on it (0.36.0,
see below); the knowledge layer lands later.

A minimal registry (owner/reviewer/curator refs are objects keyed by
`agents`, `groups`, or `roles`, resolved against your roster):

```json
{
  "schema_version": 1,
  "domains": {
    "cli": {
      "title": "CLI surface",
      "owners": { "groups": ["devs"] },
      "reviewers": { "roles": ["reviewer"] },
      "curators": { "agents": ["claude"] },
      "owned_globs": ["src/agenttalk/cli.py", "tests/test_cli.py"],
      "description": "The argparse command surface"
    }
  },
  "shared_paths": [
    {
      "glob": "pyproject.toml",
      "category": "package-metadata",
      "requires": "shared-lease-or-lead-approval",
      "default_reviewers": { "roles": ["reviewer"] }
    }
  ]
}
```

Then inspect it:

```text
$ agenttalk domain validate
domain registry: valid (1 domains, 1 shared paths)
  hash: 4e74e0e5...

$ agenttalk domain list
domains (1)  hash=4e74e0e5...
  cli  CLI surface (2 owned globs)

$ agenttalk domain check-path src/agenttalk/cli.py pyproject.toml docs/foo.md
src/agenttalk/cli.py: owned  domains=cli; casefold=true
pyproject.toml: UNOWNED  shared=pyproject.toml[package-metadata:shared-lease-or-lead-approval]; casefold=true
docs/foo.md: UNOWNED  casefold=true
```

- **`validate`** checks structure and resolves every ref against the
  roster; **`list`** summarizes domains; **`show <id>`** prints one domain
  with resolved owners/reviewers/curators; **`check-path <paths...>`**
  classifies repo-relative paths as owned / unowned / shared (with
  `--case-sensitive` / `--case-insensitive`).
- The **registry hash** is a stable, key-order-independent digest — the
  staleness keystone the later lane/knowledge phases stamp into their
  records, so a registry change can flag dependent state.

`domains.json` lives outside `messages/` and `state/`, so `agenttalk
reset` preserves it like config rather than clearing it as active bus
state.

### Lanes: a scoped deliver-gate (0.36.0)

A **lane** is an active, scoped assignment built on the domain registry:
one assignee works a subset of a domain (repo-relative path **prefixes**)
from a base SHA toward a target ref. `agenttalk lane
{assign,check,deliver,status,approve-shared}` then gates *delivering* that
work.

`lane assign --id L --domain D --assignee A --base BASE --target REF
[--path PREFIX …]` records the lane in `.agenttalk/state/lanes.json`
(active coordination state — `reset` clears it, with a warning, and never
touches the delivery artifacts below), resolving base/target to full SHAs
and stamping the current epoch + registry hash. Assign runs under a lock
and **fails closed** if the path subset overlaps another active lane.

`lane check --id L [--head H]` is read-only and prints `HOLD`/`GO` with
stable codes, exiting `0`=GO / `3`=HOLD (composing with `gate check` and
`close check`). The verdict is a pure function over resolved evidence: it
computes the changed paths from `git diff --name-status -z -M -C
base..head`, classifies each **written** path against the domain registry
using the **same segment-aware matcher** as domains (so `src/foo` covers
`src/foo/x` but not `src/foobar`), checks overlap against other active
lanes, runs `git merge-tree --write-tree` against the *current* target
head as the conflict authority (a conflict or a degraded/unavailable
result HOLDs — it never infers clean), checks epoch/registry staleness,
and consumes `gates.check_gates(scope="lane:<id>")`. A rename's old path
is in-bounds-checked (it's removed); a copy's source is evidence-only.
Hold codes: `stale_epoch`, `stale_registry`, `diff_unavailable`,
`diff_parse_error`, `casefold_collision`, `out_of_bounds_path`,
`unowned_path`, `domain_overlap_path`, `shared_path_missing_approval`,
`shared_path_wrong_approval`, `active_lane_overlap`, `merge_conflict`,
`merge_unknown_degraded`, `gate_hold`.

`lane deliver --id L` is a recoverable two-phase publication. It first writes a
signed `prepared` artifact under `.agenttalk/lane-deliveries/.prepared/`; that
file is deliberately non-consumable. It then checkpoints the lane generation,
immutable instance, candidate head, terminal evaluation, and a
`publish_pending` transaction. Only after the terminal inputs are rebound and
the final artifact is marked `committed` can close or gate consumers accept it.
Retry the same `lane deliver` after an interrupted save or publication: the
command resumes the bound transaction rather than creating a second delivery.

Worktree teardown is a later checkpoint. A committed delivery remains valid
while `cleanup_pending` or `cleanup_failed` is visible; retrying delivery (or
the scoped cleanup path) can finish teardown. Expensive Git evaluation and
worktree removal do not hold the broad config lock. A shared path still needs
`lane approve-shared --id L --path P --reason ...` from an authorized reviewer
before GO.

Release-class lanes require a provisioned worktree. `--no-worktree` is limited
to an explicitly `--advisory` lane with a recorded waiver reason. That record
is audit context only: it does not trust mutable role labels, does not prove
isolation, and cannot satisfy release isolation.

Like gates and close, lanes are **advisory, point-in-time coordination**:
a GO means "as of the current target head your work is in bounds and
merges cleanly," not a file lock, a Git/OS authorization, or a guarantee
the real merge stays clean later. Lanes **consume** gate state and never
set it. Core ships the schema, segment-aware bounds, diff parsing, the
pure verdict, and the artifact shape; the project supplies `domains.json`,
shared-path policy, lane ids/assignees/targets/subsets, and required
gates.

### Onboarding: project analysis before implementation

`agenttalk onboarding {create,list,show,state,record}` is a native ledger for
the first pass over a new project or an existing codebase. It records what the
team inspected before it starts changing code:

- **segments**: codebase areas or documentation areas assigned for reading
- **claims**: bounded statements the team believes, with source/evidence refs
- **drift**: documentation/code/runtime mismatches and their disposition
- **unknowns**: questions that are still open, optionally marked blocking

The ledger is evidence capture, not an analyzer and not proof of consensus. It
does not decide whether code or docs are "true"; agents record observed
evidence, disagreements, and open questions. Runs live under
`.agenttalk/onboarding/<run-id>/events.jsonl` as append-only JSONL. The reader
skips malformed lines, reports problems, and keeps valid records visible.

Example:

```text
$ agenttalk onboarding create --id ob-api --from claude-lead --title "API onboarding" --base-ref main
$ agenttalk onboarding record --id ob-api --from codex-dev --kind segment --key cli --status accepted --summary "CLI parser and README command reference mapped." --path src/agenttalk/cli.py --path README.md
$ agenttalk onboarding record --id ob-api --from codex-review --kind drift --key docs.cli.reference --status open --segment cli --source docs --confidence medium --summary "README command table may lag parser help."
$ agenttalk onboarding record --id ob-api --from codex-test --kind unknown --key release.owner --status open --blocking --summary "Need operator confirmation of the release owner."
$ agenttalk onboarding show --id ob-api
```

The Team Console exposes this through the **Onboarding** view and
`GET /api/onboarding`. The dashboard projection is read-only and pointer-first:
bounded summaries, paths, refs, counts, and problem rows, never raw bus message
bodies, prompt blocks, full command output, or copied source.

### Knowledge: durable pointer notes and lessons (0.38.0, lessons 0.70.0, wrapped exposure 0.71.0, dashboard learning 0.72.0)

`agenttalk knowledge {publish,curate,pull,search,onboard}` is durable,
pointer-shaped project memory hung off the domain registry. A **note**
preserves the small piece of insight that is NOT in the artifact (a seam,
a gotcha, a decision + rationale); its **anchor** points to the code or
thread that is. Consumers treat every note body as untrusted data and
reverify the anchor before acting. Notes live append-only in
`.agenttalk/knowledge/notes.jsonl` (preserved by `reset`, like
`domains.json`); the current view is the latest valid event per
`(domain_id, key)`.

**Capture is open, curation is gated.** Any active agent
`knowledge publish --domain D --type seam|gotcha|decision|pointer --key K
--anchor-kind path --path … -m "the insight"` records an *uncurated* note
(byte-capped — the body is the insight, never a copy of the anchor). A
domain owner/curator (or a lead override) then `knowledge curate
verify|retract`s it. `knowledge pull` defaults to curated, non-stale notes
(`--include-uncurated`, `--include-stale` widen it); `search` is a
substring scan; `onboard` is a bounded digest grouped by domain.

**Lessons are a note type, not a second store.** `knowledge publish --type
lesson --domain process --key K --scope review --trigger TEXT
--evidence-ref REF --review-after YYYY-MM-DD --expires-at YYYY-MM-DD -m TEXT`
captures a proposed process lesson in the same `notes.jsonl`. The reserved
virtual domain `process` is curated by the operator-facing liaison or active
lead; a lesson can also use a real code domain when domain owners/curators
should own the acceptance decision. If the registry defines a real domain named
`process`, that real domain wins: lessons published to `process` are curated by
that domain's owners/curators, and the virtual liaison/lead authority applies
only when no real `process` domain is registered. Verification moves a lesson from
`proposed` to `accepted`; `curate retract --reason ...` retires it. Default
`knowledge pull --type lesson` and `sync` consume only accepted, not-expired,
not-retired, not-superseded lessons. Lesson pulls default to five rows
(`--limit` adjusts this). `--include-uncurated` shows proposals;
`--include-stale` shows expired, retired, or superseded lessons with reasons.

`agenttalk sync --for A` includes a capped **Lessons to check** section when
active lessons match the current work context. Process-scope lessons rank
first, then the inferred scope (review/test/release/docs/craft/security),
review-due lessons, and newest accepted lessons. This is advisory memory, not
authorization: it informs the agent before acting, never blocks a command.
Repeatedly useful lessons should be promoted into skills, tests, gates, or
docs; the ledger is the capture-and-review path, not the final home.

Wrapped agents get the same advisory lesson context automatically. For
`wrap --loop`, the wrapper computes accepted lessons for the inbound message
and injects a **Lessons to check** section into the one-turn prompt; the child
model is still forbidden from running `sync`, `threads`, `drain`, `recv`,
`wait`, or `ack`. When at least one lesson is handed to the child prompt, the
wrapper appends a pointer-only exposure event to
`.agenttalk/knowledge/lesson-exposures.jsonl`: agent, message/request ids,
context scope/tags, lesson keys/refs, lesson fingerprints, and a prompt-block
hash. The event proves the operational chain (accepted -> matched ->
surfaced), not cognition or compliance, and malformed exposure lines are
skipped like malformed knowledge lines.

The Team Console exposes the same audit trail in the **Learning** view and
`GET /api/learning`. The default view shows accepted, active lessons only:
what was captured, who published it, who curated it, the owner/evidence/anchor
metadata, how often it was surfaced to wrapped agents, and recent exposure
pointers. Proposed, stale, retired, or superseded lessons stay out of the
default "accepted lessons" view and are available through explicit API status
filters for diagnostics. Exposure rows remain pointer-only and never include
raw bus message bodies, prompt blocks, or child CLI output.

**Staleness is anchor-relative**, not HEAD-relative — the make-or-break
rule, so an unrelated commit doesn't empty the layer. A note is
*hard-stale* (excluded by default) when its anchor actually changed
between `verified_against_sha` and HEAD (`git diff --name-status` on the
anchor path, the reachable-SHA check, anchor disappeared, domain/registry
hash changed, retracted) — and fails closed to stale when git can't
determine it. A moved HEAD with an *unchanged* anchor is a **caution**
(`verified_sha_not_head`), shown but not excluded; uncurated and weak
symbol evidence are cautions too. `roster --expertise` derives expertise
from domain owners/reviewers/curators + lane-delivery history + curated
note authors (raw uncurated note counts are gameable volume and excluded).

The reader is fail-safe (skips torn/invalid lines, surfaced in `doctor`,
never hiding a valid note); writes take the shared store lock. Core ships
the schema, anchor-relative staleness, JSONL contract, and CLI; the
project supplies `domains.json`, note keys/content, and curation
decisions. No vector search, mandatory index, auto-ingest, or code/doc
mirroring.

---

### Operator attention queue (0.56.0)

`agenttalk attention` is one ranked, read-only view of everything that
currently needs a human: pending `needs_operator` escalations,
config-blocked holds, dead letters, gate/close HOLDs, and unarmed
lead-loops. It **derives** the view from cheap state reads — it creates no
work object and adds no message kind — so a degraded source becomes a
bounded warning row rather than blanking the queue, and the default path
does no `git`/lane recompute. `agenttalk attention --stats` (add `--json`
for machine output) reports derived counts — what surfaced active by source,
what has been dispositioned, and the oldest active dwell — so you can see what
the queue is routing. It adds no reads beyond the attention-queue collector and
does not inspect message-body content (the collector validates message
envelopes; the counts derive only from the collected item metadata). The stats
view carries the same degraded-input warnings as the queue (a torn disposition
log or a missing liaison), so a partial read never looks complete.

**Escalations can carry typed decision fields.** `agenttalk escalate`
takes `--decision`, `--why`, `--option` (repeatable), `--recommendation`,
`--risk-if-ignored`, `--risk-severity`, `--confidence`, `--priority`,
`--needed-by`, and `--affected`, stored as a canonical nested
`meta.attention` block. Validation is strict at the CLI boundary (a
malformed field exits 2 and sends nothing); the reader is fail-safe (an
unparseable block downgrades to an untyped item with a warning, never
hiding the escalation).

**Dispositions make a decision stick.** The operator-facing liaison (or the
sole lead) runs `agenttalk attention defer|dismiss|answered-elsewhere
--item <id> --reason <text>` (`defer` also needs `--until <ISO>`); authority
resolves from `--from`/`$AGENTTALK_SELF`, no `--by`. Dispositions are
**snapshot-bound** — they hide an item only while its identifying *content*
is unchanged, so a different fault for the same agent, or an expired defer,
resurfaces automatically. `dismiss` is refused for blocking sources
(`needs_operator`, `dead_letter`, non-advisory holds): blockers get
repaired, answered, or deferred, never silenced. `--all` /
`--include-deferred` / `--include-dismissed` / `--include-resolved` widen
the view.

**Dead letters get a distinct `resolve`.** `agenttalk dead-letter resolve
--agent A --id ID --reason ...` records that a poison message was handled
out-of-band, **preserving** the payload and dropping it from the default
`dead-letter list`, the doctor warning, and the attention queue. The
central disposition log is authoritative (a best-effort `.resolved.json`
sidecar aids copied sinks). If the wrapper also spawned a matching
operator-facing "dead-letter notice" escalation, `resolve` answers that
notice thread with an audit stamp so it no longer lingers as phantom current
work. `dead-letter requeue --force-resolved --reason` reopens a resolved
item, audited. `dead-letter purge --resolved --from <liaison>` archives
resolved payloads and sidecars under `.agenttalk/dead-letter-archive/` when
you want them out of the live sink. Archived rows are no longer requeueable by
the live `dead-letter requeue` command unless you restore the archived files to
the sink.

`requeue` and `resolve` are complementary, not the same: **`requeue`
re-injects a fresh copy** (new id, own fresh attempt count) so the work gets
another try, but it **preserves the original in the sink** — so a
requeued-but-not-resolved dead letter *keeps showing* in `dead-letter list`,
`doctor`, and the attention queue. That is deliberate: we never auto-quiet a
poison message (it could hide a real unhandled failure). Once you have
actually handled it, run `dead-letter resolve --reason …` to quiet it.
Typical flow: `list` → `show` (inspect) → `requeue` (retry) → `resolve`
(when done) -> optional `purge --resolved` (archive old resolved evidence).

Dispositions live append-only in `.agenttalk/attention/dispositions.jsonl`
(latest-valid per item + action-family, fsync under the store lock,
skip-invalid on read with torn lines surfaced in `doctor`, preserved by
`reset`). v1 has no bulk/group dispositions and dedupe is display-only.

---

## Unattended operation: the supervisor and the wrapper

Everything above assumes you're at the keyboard, one terminal per agent.
agenttalk can also run agents **unattended** — a background monitor that
keeps named agents alive across provider outages, rate-limit windows, and
stuck turns, and restarts them **with their session context intact** so
they resume the branch they were on and the turn they were mid-way
through.

> **Full walkthrough: [docs/supervisor-tutorial.md](docs/supervisor-tutorial.md)** —
> scaffold, fill the config, run the monitor, wrap an agent, and trigger a
> restart-with-context, step by step. It also covers **migrating an
> existing project in and out of supervision** (it's additive and
> reversible — no data migration, no re-init).

Three ideas carry it:

- **Heartbeat freshness is the liveness authority.** Each agent stamps a
  `heartbeat` as it works and idles; a fresh heartbeat is healthy even if
  the process can't be found, and only a *stale* one (older than
  `stuck_after_seconds`) triggers recovery. No fragile find-the-PID dance
  decides life-or-death.
- **The supervisor is an external monitor, not a daemon.** `agenttalk
  supervise --report`/`--plan` are read-only derivations; a generated
  `supervisor.ps1` polls the plan and does the launching/relaunching/
  scoped-killing. The bus stays just files.
- **Every restart resumes the agent's session**, so a relaunched agent
  still knows what it was doing — via a pinned id for manual Claude
  (`--resume <id>`), `resume --last` in its `CODEX_HOME` for manual Codex,
  or the wrapper's own persisted id/`thread_id` for wrapped agents. The
  tutorial spells out all four paths.

For durable unattended listening, use a supervised `agenttalk wrap
--loop` process as the documented default. A manual chat-window listener
is best-effort: host CLI behavior, context compaction, and terminal
lifecycle can interrupt the wait loop. This matters most for Claude Code,
where in-window background waits can be reaped; unattended Claude agents
should be wrapped. Codex manual listening is a tolerable stopgap when a
human is watching, but the honest unattended pattern is still the
wrapper. Listening and wakes affect latency, not correctness state:
queued messages stay durable, and `sync` / `threads` rebuild obligations
after a restart.

Quick start:

```powershell
agenttalk supervise --init                 # scaffold .agenttalk/supervisor.{json,ps1}
# fill in supervisor.json: per-agent launch command, cwd, cli
agenttalk supervise --install-activity-hook  # (manual-listen agents) unlock stuck-recovery
agenttalk supervise --install-activity-hook --interactive-for claude  # human liaison window
.\.agenttalk\supervisor.ps1                # run the monitor in its own terminal
agenttalk supervise --bootstrap-check       # verify roster + wrapped Claude/Codex liveness
agenttalk request-restart --for codex-dev  # bounce an agent on demand (resumes its session)
```

`--bootstrap-check` is the ready-to-assign preflight. It emits JSON and
checks that roster names are not just inert identities: an operator-facing
liaison exists, supervisor agents are in the roster, wrapped Claude/Codex
commands include explicit `--root`, launch placeholders are gone, managed
agents have fresh heartbeats, and stale roster-only names are called out so
you can supervise, retire, or deliberately ignore them.

On Windows, supervised agents launch hidden by default to keep unattended fleets
from opening a console per agent. Set top-level `"window_style": "minimized"` or
`"normal"` in `.agenttalk/supervisor.json` to change the default, or set
`window_style` inside an agent block (or ephemeral reviewer profile) to override
one agent. Valid values are `hidden`, `minimized`, and `normal`; invalid values
fall back to `hidden` with a supervisor warning. For wrapped agents, hidden mode
also tells the wrapper to spawn its CLI child without a visible console.

An agent becomes stuck-recoverable in one of two ways: a normal
`/agenttalk.listen` agent **with the activity hook installed**, or an
agent run **through the progress wrapper**. Until an agent can confirm
"stuck" (hook or wrapper), a stale heartbeat is **warn-only — never a
kill**, so an un-instrumented agent is never mistaken for stuck.
The ordinary activity hook is identity-neutral and stamps whichever
agent is in `AGENTTALK_SELF`; use it for managed/manual agents launched
with that environment. For the human's interactive Claude liaison window,
install `agenttalk supervise --install-activity-hook --interactive-for
<lead>` so the hook has an explicit fallback identity when
`AGENTTALK_SELF` is absent. Non-liaison interactive windows should not
use the fallback form; set `AGENTTALK_SELF` instead.

### The progress wrapper — visibility + working-turn heartbeat

`agenttalk wrap` runs an agent through a per-CLI structured-stream
adapter that gives you three things a bare supervised process can't:

- **visibility** — it echoes the agent's stream to the console
  (token/thinking deltas for Claude; item-level events for Codex), so a
  background agent isn't a black box;
- **a working-turn heartbeat** — it heartbeats while the agent is
  *working*, not just idling, so a long honest turn never looks stuck;
- **degraded-output detection** — a confirmed garble-then-silence can
  request a self-restart.

```powershell
# long-running supervised wrapper: idle on the bus, drive one turn per inbound message
agenttalk wrap --for codex-dev --cli codex --loop -- `
  "C:\path\to\codex.exe" -a never -s workspace-write -C "D:\Projects\example"
```

A wrapped agent is instrumented by construction (no activity hook
needed) and owns session continuity end-to-end, so a supervisor relaunch
re-runs the identical command and reload-resumes the session. For
hands-off durable listening, wrapping is the documented default. Manual
`/agenttalk.listen` remains supported for interactive work, and a Codex
chat window is a tolerable supervised-by-human stopgap, but it is not a
daemon. Per-CLI stale thresholds differ on purpose (wrapped Claude
streams through reasoning → 180s; wrapped Codex is item-level and silent
during pure reasoning → 900s+); see the tutorial for the threshold
guidance and the guardrails.

**Bounded work heartbeat (wrapped Claude).** Streaming progress is not
the only legitimate quiet: a long non-streaming stretch (a big tool call,
a slow API turn) stamps nothing, and a wrapped Claude could be falsely
stuck-recovered mid-turn at its tight 180s threshold. The wrapper
therefore runs a small **bounded ticker** during each turn: it stamps the
same supervisor heartbeat every `interval_seconds` (default 30) while the
per-turn child process is alive, but only up to `max_turn_seconds`
(default 900) — past that cap only real progress refreshes liveness, so a
genuinely hung silent turn is still recovered at
`max_turn_seconds + stuck_after_seconds`, never masked forever. A failed
turn still ends with **no** fresh heartbeat (the ticker is stopped, with
in-flight stamps synchronized, before the failure-path clear). Default-ON
for wrapped Claude (`wrap --loop`, `--lead-loop`); default-OFF for
wrapped Codex (its long thresholds and turn-watchdog math are unchanged)
and for `--one-shot`. Configure per agent (or globally) in
`supervisor.json`: `"work_heartbeat": {"enabled": true,
"interval_seconds": 30, "max_turn_seconds": 900}`. Guards fail visibly
at launch (config-blocked hold) — a non-positive/non-numeric value, or an
interval above `min(60, stuck_after/3)` without
`allow_high_interval=true`, is refused, never silently coerced. A
best-effort diagnostics record lands in
`state/work-heartbeat/<agent>.json` for doctor/status forensics; the
supervisor does not read it.

Supervisor state is persisted as `supervisor-state.json` with a validated
`.bak` generation. Readers prefer a valid primary, fall back to a valid backup
without rewriting a corrupt primary, and fail closed when both copies are
invalid. Heartbeats farther in the future than the configured skew allowance
cannot establish freshness; timestamps within that bounded allowance can.

Wrapper waiting markers are generation-bound. Each loop writes a unique token
and clears the marker only when its token still matches, so an exiting old
wrapper cannot erase a replacement's live marker.

On Windows, the per-turn watchdog no longer launches `taskkill.exe`; verified
targets use `os.kill(pid, signal.SIGTERM)`, which maps to abrupt process
termination rather than graceful SIGTERM handling. This eliminates the
`taskkill.exe` subprocess path that produced the reported popup. The production
reporter's desktop-heap exhaustion diagnosis is plausible but is not an
upstream-confirmed root cause. Windows snapshot and start-time helpers still
launch PowerShell/CIM subprocesses, PID reuse remains possible after the
recheck, and the leaf-first snapshot operation is not an atomic tree kill.
Those limitations are follow-up hardening, not blockers for this narrow fix.

Protected agents — the operator-facing liaison and every active
`role=lead` — are **never auto-killed** (warn/note only), and a manual
`request-restart` of one needs `--force-protected`; if that protected agent
still has a fresh heartbeat, the operator-facing requester must also pass
`--acknowledge-live-protected-kill`.

---

## CLI reference

| Command | What it does |
| --- | --- |
| `agenttalk init [--here] [--agents A,B]` | Create `.agenttalk/` in the current dir. Refuses to create a **nested store** when one exists up-tree (0.14.0) — `--force` for a deliberate sandbox. |
| `agenttalk roster [--json]` | Show agents, roles, group memberships, and the resolved current identity. |
| `agenttalk roster add <name> [--role R] [--group G]...` / `remove <name> [--force]` / `set-role <name> <role>` / `set-group <group> <a,b,c>` | Deliberate roster/group admin operations. Groups are validated roster subsets; `all` is implicit and reserved. 0.16.0: `add` refuses a retired-tombstone name; `remove` is refused by default with a retire hint — `--force` removes anyway and warns that history-read breaks (no tombstone — re-addable). |
| `agenttalk roster retire <name> [--reason R]` / `rename <old> <new> [--drain-check] [--reason R]` / `forward <retired> --to <live> --to-request RID [--from A]` | Identity lifecycle (0.16.0, #19). `retire` makes a **permanent tombstone** (can't send, name never re-bound, history stays valid). `rename` = retire `<old>`→tombstone + add `<new>`, carrying over role/group/operator-facing; `--drain-check` refuses while work is owed to/from `<old>`. `forward` redirects a single owed request to a live agent, transcript-visible. |
| `agenttalk barrier bump --from A --scope global [-m REASON] [--json]` | Fire a **global epoch barrier** (0.16.0, #19): one meta-marked message whose id becomes the new epoch, marking everything before it as a previous epoch. Any active member may bump (trusted-team global-stall lever). Tracked openers after it record `epoch_at_send` automatically. |
| `agenttalk domain [--json] {list,show <id>,check-path <paths...>,validate}` | Inspect the project's **domain registry** (`.agenttalk/domains.json`, 0.31.0). `list` = domains + registry hash; `show <id>` = one domain with resolved owner/reviewer/curator refs; `check-path <paths...>` = classify repo-relative paths as owned/unowned/shared (`--case-sensitive`/`--case-insensitive`); `validate` = structure + ref resolution. Read-only foundation for upcoming lane/knowledge features; author `domains.json` by hand for now. |
| `agenttalk onboarding {create,list,show,state,record}` | Track a new-project or existing-codebase analysis pass before implementation. Runs live under `.agenttalk/onboarding/<run-id>/events.jsonl` and record bounded segments, claims, drift, and unknowns with pointer evidence. Evidence tracking only: not an analyzer or consensus proof. |
| `agenttalk whoami [--for A] [--json]` | Show effective root, resolved self and peer, roster membership, role/groups, unread count, and owed-thread count. Warns when identity is unresolved or not in the roster, which is often a wrong `--root` or env issue. |
| `agenttalk status` | Show roster, per-agent cursor, unread count, and **actionable warnings**: never-acked unread, soft-deadlocks, unconsumed correlated replies, and stale outbound threads. |
| `agenttalk threads [--for A] [--all] [--json]` | Derive request/reply thread state from validated messages. Default view shows actionable rows only (`reply-waiting`, `owed-inbound`, `open-outbound`); `--all` includes `closed`. 0.16.0: open rows in `--json` carry read-only `next_owner` / `next_action` (`reply`/`read-reply`/`await-reply`/`answer-operator`) — who owes the next move, a pure projection of state. |
| `agenttalk sync --for A [--lesson-tag TAG] [--json]` | Rejoin digest for manual/rejoining agents: show identity, roster, actionable threads grouped by request id, terminal decisions, recent unread non-action messages, deterministic next-action hints, and a capped advisory `Lessons to check` section when accepted lessons match the work context. Pure derivation; no cursor, threadstate, or lesson-exposure writes. Wrapped agents receive matched lessons through the wrapper prompt path instead. |
| `agenttalk capacity [show\|refresh] [--for A] [--source auto\|claude\|codex] [--threshold N] [--context-threshold N] [--reset-soon-min N] [--statusline-path PATH] [--sessions-dir PATH]` | Advisory headroom snapshots. `refresh` reads the caller's local Claude/Codex signal and publishes a normalized snapshot for `A`; `show` (the default) prints the team's published 5-hour/weekly usage, context-window fill, stale/unknown confidence, and near-cap/reset-soon/near-compaction flags. Never gates progress. |
| `agenttalk avatar list\|set\|clear\|set-operator\|clear-operator` | Display-avatar preferences. Choices are allowlisted ids, never filenames or URLs. The shaped avatar families (`hexagon-*`, `oval-muted-*`, `oval-vivid-*`, `rounded-square-*`, `star-*`, `triangle-*`) are opt-in self-select variety via `agenttalk avatar set <shape>-<name> --from <self>`; role defaults stay on the original circular robot/operator avatars. |
| `agenttalk send --from A --to B [--kind K] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -)` | Drop a message into the bus. `--file -` reads the body from stdin. `review-request`, `question`, and `proposal` without `--meta request_id=...` get one minted + printed; `wake` gets a `wk-` correlation id minted the same way but does **not** open a thread (0.24.0); `review-result` and `proposal-response` without one warn (soft, exit 0). |
| `agenttalk broadcast --from A (--to-group G \| --all) [--kind message\|note\|question] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -) [--print-id] [--quiet]` | Fan out one message per recipient, excluding the sender. Mints `broadcast_id=b-...`, stores it as `meta.broadcast_id` and `meta.request_id`, and prints the recipient list unless quiet. 0.15.0: `--to-role <role>` targets every member holding a role (frozen into each copy at send time); a PARTIAL fan-out exits **5** with a delivered/missed manifest — recover with `--resume <bid>` or rescind. |
| `agenttalk propose [--from A] [--to B] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -) [--in-reply-to ID] [--print-id] [--quiet]` | Send a first-class `proposal`. Auto-mints `meta.request_id=pp-...` if absent and prints `(proposal id: pp-...)` unless quiet. `--in-reply-to` sets `meta.in_reply_to` for counters. |
| `agenttalk recv --for A [--ack] [--since ID] [--include-control]` | **Peek** at queued messages — does NOT move the cursor unless `--ack`. Plain `recv` that prints messages emits a hint pointing at `drain`. Hides `composing` pings by default; `--include-control` surfaces them. |
| `agenttalk drain --for A [--include-control]` | **Consume**: print all unread AND advance the cursor to newest, in one shot. Same path as `recv --ack`. Use this instead of hand-rolled timestamp polling. |
| `agenttalk wait --for A [--to-request RID] [--kind K] [--timeout 120] [--no-ack] [--grace 2] [--composing-extend 120] [--max-poll-interval 2.0] [--refuse-stacked-wait]` | Plain wait blocks until a new real message arrives, prints it, and advances the global cursor unless `--no-ack`. Scoped wait (`--to-request` and/or `--kind`) returns only matching addressed messages, advances only the per-thread `seen_msg_id`, and never advances the global cursor. A scoped wait on a rescinded request wakes immediately with **exit 3** (0.14.0). Idle polling backs off from `--interval` up to `--max-poll-interval` (reset on activity; set `<= --interval` to disable). `--refuse-stacked-wait` exits **6** instead of warning when a live duplicate waiter already holds the mailbox; an older scoped wait also exits **6** with a stderr superseded diagnostic when a newer same-thread waiter replaces it. |
| `agenttalk composing --from A [--to-request RID] [-m "still drafting"]` | Send a `composing` ping so the peer's `wait` extends its deadline. Use periodically while you draft a long reply. The peer's `wait` consumes these as deadline-extension signals — they do NOT surface as a returned reply. With `--to-request` (0.14.0) the peer is derived from the thread, and a **reply-in-flight** marker shows up in their `threads`/`sync`. |
| `agenttalk ack --for A [--id ID] [--to-request RID]` | Without `--to-request`, manually move an agent's global cursor forward. With `--to-request`, manually close that request thread for A and record the latest seen matching message without touching the global cursor. |
| `agenttalk rescind --from A --to-request RID [--to-id MSG] [-m REASON]` | Mark a tracked request you opened as **no-longer-current** (0.14.0). Transcript-visible; the thread becomes `closed-superseded`, a peer blocked in `wait --to-request` wakes with exit 3, and `check` reports superseded. Requester-only. Prefer this over a prose "ignore my last message". |
| `agenttalk check --for A --to-request RID [--epoch] [--json]` | **Pre-action currentness gate** (0.14.0): prints `current`/`superseded`/`unknown`, exits 0/3/4. Run it immediately before any irreversible action tied to a request — exit 3 is a hard stop. Read-only; a local `ack` never masks a rescind. 0.16.0 `--epoch` also checks the global epoch: exit **3** if the request predates the latest barrier (previous-epoch, or a pre-epoch opener to re-ask). Adds an additive `epoch` object to `--json`. |
| `agenttalk escalate --from A (-m TEXT \| --file -) [--to X]` | Route an operator question to the **liaison**, falling back to the single `role=lead` agent when no liaison is configured (0.24.0). Resolution: `--to` → liaison → sole lead → refuse. Mints an `esc-` request_id (printed as `request_id=<id>`); refuses (exit 2) only when none of those resolve, with a remediation naming both `set-operator-facing` and `set-role … lead`. |
| `agenttalk roster set-operator-facing <name>` / `--clear` | Designate the ONE agent the human operator talks to directly (0.14.0). Advisory routing metadata, single slot — "two liaisons" is unrepresentable. |
| `agenttalk prune --invalid [--dry-run] [--json]` | Quarantine invalid message files into `.agenttalk/quarantine/` (0.15.0) — move-only and **recoverable** (restore = move the file back); the selection is exactly what status reports as INVALID; valid files untouched by construction. |
| `agenttalk transcript [--format md\|jsonl] [--out PATH]` | Export the full conversation. |
| `agenttalk end --from A [--reason ...]` | Notify the other agent(s) and write the transcript. In a team, sends `end` to every other roster member. |
| `agenttalk release --from A (--to B \| --to-group G \| --all) [-m reason]` | Signal an agent (or team) to **stand down and exit its listen loop** — distinct from `end`: no transcript export, and the agent may be restarted later. A listener exits ONLY on `kind=release` or `kind=end`; a prose "done for now" never stops it. A single `--to` opens no thread (no `request_id`/`broadcast_id`); `--to-group`/`--all` fan out the same signal (re-run to retry any missed — no `--resume`). Authoritative only from the `operator_facing`/sole-`lead` sender; the command warns otherwise and the listen skill reports-and-ignores an unauthorized release. |
| `agenttalk reset [--archive]` | Clear **active bus state** (messages + cursors + heartbeats); preserves historical transcripts under `.agenttalk/sessions/` so past exports aren't lost. Bumps `session_id`. With `--archive`, instead moves **everything** (messages + state + sessions) under `.agenttalk/archived/<old_session>/`. Preserves config (roster) either way. |
| `agenttalk supervise (--init \| --report \| --plan \| --bootstrap-check \| --install-activity-hook \| --clear-restart)` | Thin support for the **external agent supervisor** (24/7 outage auto-restart + stuck-recovery). `--init` scaffolds `.agenttalk/supervisor.{json,ps1}` (a config you fill with per-agent launch commands + a generated PowerShell monitor script; POSIX is a follow-up). `--report`/`--plan` emit the read-only liveness JSON and the **action plan** (the shared decision table the script executes). `--bootstrap-check` emits a JSON ready-to-assign preflight for the roster, operator-facing lead, supervisor config, wrapped Claude/Codex launch invariants, explicit wrapped `--root`, and fresh heartbeats. Heartbeat freshness is the liveness authority: a fresh heartbeat is healthy even when process discovery is missing or misleading; a stale heartbeat becomes `stuck_recover` (best-effort kill + resume) only when the activity hook is installed (`activity_hook=true`), else it is warn-only (`suspect_warn`), never a kill. `--install-activity-hook` merges the identity-neutral `agenttalk heartbeat --hook` PostToolUse hook into the **project** `.claude/settings.json` (`--codex` for `.codex/hooks.json`; never global, never clobbers). Add `--interactive-for <lead>` only for the current operator-facing human Claude liaison; it writes a Claude-only fallback hook and refuses Codex hook modes. Every recovery **resumes the pinned session** so context survives a force-kill. Protected agents (`operator_facing` ∪ every active `role=lead`) are never auto-killed (warn/note). |
| `agenttalk wrap --for A --cli claude\|codex [--loop] [--no-render] [--from S] [--min-interval N] -- <real-exe> <base-args>` | Run agent `A` through the **progress wrapper** (0.30.0): a per-CLI structured-stream adapter giving **visibility** (echoes the agent's stream — token/thinking deltas for Claude, item-level events for Codex; `--no-render` to silence), a **working-turn heartbeat** (stays fresh while the agent works, not just idles), and **degraded-output detection** (confirmed garble-then-silence can request a self-restart, recorded as `--from`). `--loop` makes it the long-running **supervised** wrapper: it owns the idle bus-wait + heartbeat and drives the CLI **one turn per inbound message**, persisting+reloading the Codex `thread_id`/Claude `session-id` so a relaunch reload-resumes. Each inbound wrapped turn also receives matching accepted lessons as advisory prompt context and records pointer-only exposure telemetry after prompt handoff. The real CLI exe + its base args go after `--`; the wrapper appends the per-turn session/stream args. For durable unattended listening, this is the documented default; manual `/agenttalk.listen` is best-effort for interactive use. |
| `agenttalk request-restart --for A [--from L] [--reason ...] [--force-protected] [--acknowledge-live-protected-kill]` | Queue a **manual** restart of agent `A`: writes an atomic, request-id-scoped `state/<A>.restart-request` marker the supervisor relaunches (resuming the session) from and clears. Healthy idle agents are eligible for manual restart at the next supervisor poll. Restarting a protected agent requires `--force-protected`; if that protected agent still has a fresh heartbeat, the operator-facing requester must also pass `--acknowledge-live-protected-kill`. |
| `agenttalk heartbeat [--for A] [--min-interval 5]` | Stamp this agent's **activity heartbeat** (the supervisor's stuck signal). Wire as a Claude PostToolUse / Codex hook so it's stamped at **tool boundaries** (PostToolUse) **plus** the wait-loop heartbeat while idle — so it stays fresh whether the agent is waiting or running tools, and goes stale only when the model is genuinely stuck. Hook identity resolves from `--for`, then `AGENTTALK_SELF`; the installer uses the hook-only fallback form for the operator-facing interactive liaison. Choose `stuck_after_seconds` generously for the longest expected no-tool model/API turn or long-running tool call; production configs may need a larger value than the 120s default. **Throttled** — a no-op if the heartbeat is younger than `--min-interval`, so the per-tool-call hook costs almost nothing. |
| `agenttalk compact [--dry-run] [--keep-count N] [--keep-age-days D] [--json]` | Bound live-store growth by archiving a **safe prefix** of old messages (`id < keep_floor`) into the cold `.agenttalk/archived/compacted/` dir. `keep_floor` is the MIN of: the lowest active cursor (never archive a message unread by an active recipient), the current epoch barrier, the earliest message of any **protected** thread (owed-inbound / reply-waiting / open-outbound / closed-superseded — kept whole), and a keep-tail (`keep_count` newest + everything younger than `keep_age_days`). Any undeterminable component fails safe to **archive nothing**. Never archives invalid files (they stay for `prune`/`doctor`). Diagnostics name which component capped the floor; `--dry-run` plans without moving. |
| `agenttalk hmac-init [--force]` | Generate the HMAC signing key for this project. Stored outside `.agenttalk/` (per-user config dir). The key's existence at the path-derived per-user location automatically activates signature enforcement — there's no config flag to flip. Override the default key path with `AGENTTALK_HMAC_KEY_FILE`. See `SECURITY.md`. |
| `agenttalk reply [--from A] [--to-id MSG_ID \| --to-request REQUEST_ID] [--kind K] [--subject S] [--meta k=v] (-m TEXT \| --file PATH \| --file -) [--dry-run]` | Reply to the most recent received message, or anchor to a specific received message/thread. Auto-derives recipient and echoes the anchor's `request_id`; explicit `--meta request_id=...` wins. `--dry-run` prints the resolved recipient, request id, and kind without sending. A reply that opens a new thread (`review-request` or `proposal`) mints a fresh id instead of echoing. 0.15.0: `--na` sends a not-applicable response — closes your obligation, displayed as (n/a); refused on review-request/proposal threads. |
| `agenttalk tail [--from-start] [--interval S] [--timeout S]` | Passive monitor: stream all messages as they arrive. Does **not** advance cursors or write heartbeats — safe to run in a third terminal alongside two active agents. `--from-start` replays existing messages first. |
| `agenttalk serve [--port P] [--host H] [--access-log]` | Start a **read-only** local web dashboard at `http://127.0.0.1:8765/` for browsing the message log in a real browser. **Loopback-only by design** — only `127.0.0.1`, `::1`, and `localhost` are accepted; there is no flag to expose it elsewhere (SSH-tunnel `localhost:<port>` from another machine if needed). HTML output is escaped, strict CSP, `GET`/`HEAD` only, peer-IP check on every method. JSON at `/api/status` and `/api/messages` for scripting. 0.17.0: the same server also serves `/dashboard` (the obligation view) and `/api/state`; a port that can't be bound now exits **2** with a `--port 0` hint instead of a raw traceback. See `SECURITY.md`. |
| `agenttalk dashboard [--port P] [--store PATH]... [--access-log]` | The **obligation dashboard** (0.17.0): who owes what, whose turn it is, and the next action — per agent, per open thread, with mission/WP tags and epoch staleness. Same read-only loopback-only server as `serve`, landing on `http://127.0.0.1:8765/dashboard`; auto-refreshes state every ~2 s. Repeat `--store <project-root>` to watch **several projects in one tab** (each path is the project root itself — no upward search; an uninitialized path shows as a degraded panel, not an error). No `--host` option exists on this spelling. `GET /api/state` (`schema_version: 1`) is the envelope data for scripting; `GET /api/learning` returns the selected root's accepted lesson ledger plus pointer-only exposure telemetry; `GET /api/onboarding` returns selected-root onboarding runs and bounded evidence pointers. |
| `agenttalk install-skills [--claude-only\|--codex-only] [--no-devkit\|--devkit-only] [--force] [--dry-run]` | Copy bundled bus skills to `~/.claude/commands/` and `~/.codex/skills/`, and by default copy the shared dev-discipline devkit (`craft-code`, `test-coverage`, `review-code`, `write-docs`, `review-docs`) to both `~/.claude/skills/` and `~/.codex/skills/`. `--claude-only` and `--codex-only` scope only the bus skills; use `--no-devkit` to skip the shared devkit. Idempotent — preserves your local edits unless `--force`; use `--dry-run --force` to preview overwrites. |
| `agenttalk codex-config [--enable\|--disable\|--status]` | Manage per-project sandbox/trust block in `~/.codex/config.toml` so Codex can call agenttalk from inside its sandbox. |
| `agenttalk doctor [--json]` | Health check: store initialized, bus skills installed + in sync, devkit absent/in sync/stale state surfaced, Codex sandbox block configured, heartbeats fresh. Per the global exit-code contract, exit 2 on any error; warnings exit 0 with the warning state visible in output. |
| `agenttalk status --json` | Structured status output for automation (consult freshness, external tooling). Same data as plain `status` plus `invalid_messages[]`, `warnings[]`, per-agent `waiting` / `waiting_stale`, and thread-derived warning state (additive — existing keys unchanged). |
| `agenttalk --version` | Print the installed version. |

### Assurance gates and approved review evidence

Use `agenttalk gate {set,list,check,waive}` to manage lightweight
assurance state in `.agenttalk/gates.json`. Required gates default to
empty until a project opts in. `gate check` prints top-line `GO` or
`HOLD`; it exits 3 when an unwaived `severity=blocker` gate is `red` or
`unknown`. A blocker gate can be set `green` only with
`--evidence-source automation_ci`; operator waivers must use `gate waive`,
which records the operator, date, reason, scope, and expiration.

Use `agenttalk check --for A --to-request RID --gates` immediately
before release, merge, tag, or milestone-close actions that must respect
gate state. It keeps the existing currentness/rescind behavior and adds
a `HOLD` failure when gates block. With `--json`, the output includes an
additive `gates` object.

A `review-result` with `--meta status=approved` must include typed
evidence metadata: `risk_class`, `release_blocker`,
`tests_referenced`, `tests_executed`, `evidence` or `artifacts`, and
`residual_risk`. Use `na_reason` when any field is `n/a`. A lightweight
approval can use `risk_class=none`, `release_blocker=no`, `n/a` evidence
fields, and a short `na_reason`.

Response statuses are exact enums. `review-result` accepts
`approved|rejected|needs-info`; only `approved` and `rejected` are terminal.
`proposal-response` accepts `accepted|rejected|countered`, all terminal. A
mixed-version message with no status remains readable but nonterminal; a
present unknown or wrong-typed status is refused on write and skipped on read.

### Milestone/release close (`agenttalk close`)

`agenttalk close {open,ack,draft,counter decide,check,publish,reopen,list,show}`
aggregates the assurance signals above — gate state, typed review evidence,
and named-gate-bound remediation — into one auditable `HOLD`/`GO` verdict for a
frozen revision, gathered from a declared set of required review **lenses** and
published by a lead. State is a per-close atomic JSON file in
`.agenttalk/closes/<id>.json`. It is **opt-in** (no required lenses or gates by
default) and **advisory**: like gates, agenttalk authenticates the sender but
does not enforce identity, so close records *who acted* and is a strong release
signal + audit trail, never an enforced lock. The bus carries the evidence text;
the close file stores pointers, not copies. Every update of an existing close
is serialized by close id and compares both a monotonically increasing
`generation` and an immutable `instance_id`. A stale writer, a missing token,
or a delete/recreate ABA therefore fails closed and must reload. Creation is
exclusive; `open --force` performs a locked replacement with a new instance.
Legacy records are upgraded under that same lock and are never overwritten by
an unchecked compatibility path.

`close open --id ID --scope release --revision REF --lens NAME --allow NAME:AGENT`
freezes `REF` to a full SHA via git (a dirty worktree needs `--dirty-artifact`
or stays `HOLD` on revision) and declares the required lenses. `--allow
NAME:AGENT` authorizes an agent, `--allow NAME:@ROLE` a role; an ack from anyone
else is `HOLD` (`unauthorized_lens_ack`) unless a lead records `--override`.

`close ack --id ID --lens NAME --status accept|counter|na` records a lens
verdict. `accept` reuses the 0.32.0 typed evidence (`--risk-class`,
`--release-blocker`, `--tests-executed`, `--evidence`, …); `na` needs a
`--reason`; `counter` raises a finding that holds the lens until the lead
decides it with `close counter decide --decision accept|reject --reason …`.
Accepting a counter records a remediation item; a `--blocker` remediation **must
name a `--gate`**, and `GO` then requires that gate green from `automation_ci`
or an operator waiver — gates remain the single resolution authority (close
never creates or mutates gates).

`close check --id ID` prints `HOLD`/`GO` with stable hold codes and exits
`0`=GO / `3`=HOLD, matching `gate check` so they compose in automation. The
verdict is a pure function over (close record, gate check): `GO` requires a
well-formed record on a frozen, clean (or dirty-with-artifact) revision, a gate
`GO`, every required lens satisfied by an authorized non-stale ack, every
counter decided, and every accepted blocker remediation resolved by its gate. A
revision change stales prior acks (they reviewed different code).
When reopen changes the revision, it also clears the prior dirty-worktree
artifact so evidence for the old revision cannot be reused.

`close publish --id ID --from LEAD --verdict go|hold` records the terminal
snapshot; a `go` is refused unless `check` is `GO`. Post-publish acks are
rejected until a lead runs `close reopen`, so a close is stale-proof **without**
a team-wide epoch bump. The global epoch is audit-only at open; only an explicit
`close publish --verdict go --bump-barrier` recomputes gate, sign-off, and
worktree evidence while holding the cooperating close serialization boundary,
then persists GO with a barrier binding containing close id, instance id,
revision, and generation. Sending the barrier and stamping its epoch are
recoverable steps. If either fails, retry the identical publish command: it
finds the uniquely matching validated barrier and stamps it, or sends exactly
one when absent. Duplicate matching barriers or changed bindings fail closed.
This is an idempotent recovery protocol, not one ACID transaction across the
close file and message bus. A `hold` never bumps the barrier.

### Specialist sign-off by risk class (`agenttalk close signoffs`)

`agenttalk close signoffs {plan,apply,override}` turns the close's explicit
lenses into review **sign-offs routed from the risk classes in play**. It is
opt-in (no `.agenttalk/signoffs.json` policy = zero derived signoffs) and, like
everything in `close`, **advisory**: it counts who signed and whether the
required counts are met; it is not an access-control boundary.

The project owns the policy in `.agenttalk/signoffs.json`: per risk class, a list
of signoff sets `{id, required_count, candidates: {agents, groups, roles},
use_default_reviewers, include_domain_reviewers, allow_na, countable_statuses,
override_counts}`, plus `defaults.reviewers` and `allow_unmapped`. The core
**validates** the policy and the risk-class strings (the envelope `none`,
`unknown`, `release`, `device`, `accessibility`, `security`, `performance`,
`persistence`, `docs-contract`, `quality`, plus `project:...` extensions) but
never **decides** a change's risk —
the lead/project supplies the close's risk inventory.

Policy flags `use_default_reviewers`, `include_domain_reviewers`, `allow_na`,
and `override_counts` must be literal JSON booleans; strings such as `"false"`
are invalid. Every `counter_id` must be unique across the entire close, even
when counters come from different lenses.

`close open … --derive-signoffs --risk-class X` (or `close signoffs apply`)
freezes the route: it records the policy hash, the risk-inventory hash, and the
revision, derives first-class `required_signoffs`, and generates `required:false`
signoff lens slots. Changed paths default to `git diff --name-only
<base>..<revision>` (the frozen revision; `--changed-path` is an audited
override). Crucially it **freezes the route inputs, not the people**: candidate
refsets resolve against the *current* roster/groups/roles (and, additively, the
matched domains' `reviewers` from `domains.json`) at check time, so a reviewer
added or removed later is honored without reopening; only a policy / risk /
revision change raises `stale_signoff_route` until you re-apply.

A set is satisfied by enough **distinct, currently-qualifying** acks (`close ack
--lens <generated-id>`): one agent cannot satisfy `required_count=2` with two
acks, a non-candidate ack is refused, `na` counts only when the set sets
`allow_na` (and carries a reason), a `counter` does not count unless listed in
`countable_statuses`, and a lead `--override` ack does not count unless the set
sets `override_counts`. `close check` adds the stable HOLD codes
`missing_required_signoff`, `unroutable_required_signoff`, `invalid_signoff_policy`,
`unmapped_required_risk`, and `stale_signoff_route` (still exit 0=GO / 3=HOLD). An
unroutable or otherwise blocked set has exactly one escape: `close signoffs
override --set ID --from LEAD --reason …` (close-lead authority; recorded and
audited, never counted as a specialist sign-off).

The verdict stays **pure**: the CLI does all the I/O (load the policy, resolve
refsets against roster/domains, run `git diff`, hash the route) and hands
`compute_verdict` a resolved evaluation; the core only counts. Rubric content,
automatic specialist discovery, and risk inference from code remain out of scope.

### Ephemeral evidence reviewers

`agenttalk request-launch --from LEAD --profile PROFILE --skill SKILL --revision REF
[-m PROMPT | --file PATH] [--path P ...]` queues an evidence-only adversarial
review for the external supervisor. The command freezes `REF` to a full 40-char
SHA and writes a data-only marker at
`.agenttalk/state/launch-requests/<request_id>.json`.

The supervisor must opt in with `ephemeral_reviewers.enabled=true` in
`.agenttalk/supervisor.json`. It validates the marker before claim: strict
requester authority (operator-facing agent, else the sole active `role=lead`;
no zero-lead fallback), allowed profile/skill/role/groups, prompt-size cap,
rate/concurrency caps, and optional `current_revision` staleness. Denied markers
are archived and are not retried forever.

Accepted markers roster a fresh `adversary-*` identity, send it one
`review-request`, launch `agenttalk wrap --loop --one-shot --to-request <id>`,
and retire the identity immediately after terminal evidence, failure, or timeout.
The wrapper gets a fresh prompt/session/home in v1; this is prompt/session
freshness, not hard OS isolation. Reviewed code and marker prompts are untrusted
data.

Completion is evidence-only: `review-result status=approved` can support a gate
but is never a counted signoff, `status=rejected` is a counter/remediation
signal, and `needs-info`, malformed output, or no typed result keeps the request
on HOLD.

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
- `closed`: a terminal correlated response exists (`review-result`
  `approved|rejected`, `proposal-response` `accepted|rejected|countered`, any
  non-control question answer), or a local ack closed the view. `needs-info`
  remains nonterminal and moves the ball back.

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

For `review-result`, `status=approved` also requires typed evidence
metadata; see "Assurance gates and approved review evidence" above.

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
stay live. Each root carries a stable path-derived `project_id`; the display
label is not a write-routing key. Selected-root Team Console responses return
`root_info` with `project_id`, label, and full path. GET routes may omit `root`
to select `root[0]` and may accept a unique display label as a legacy
best-effort selector. A blank, repeated, unknown, or ambiguous GET selector
returns HTTP 400 `bad_root`.

Writes are stricter. When several roots are served, POST `/api/intent` and
`/api/lead-chat` require exactly one explicit full `?root=<project_id>`; label
fallback and omission are forbidden. Omission is accepted only in single-root
mode.
Blank, repeated, unknown, ambiguous, or non-full selectors return HTTP 400
`bad_root` before any mutation.
Thread rows carry subjects and derived fields only, never message bodies; raw
thread bodies are available through the selected-root `/api/thread/<id>` and
lead-chat transcript surfaces. If a watched project contains `kitty-specs/`,
the panel lists its missions — detection is filesystem-only, agenttalk
never imports spec-kitty.

The top bar always shows current project and path context, even for a single
watched root. If CSS ellipsizes the path visually, its complete value remains
available through the element text, title, and accessibility label. Duplicate
basenames receive stable project-id suffixes. Switching projects clears
root-bound drill-ins, caches, and action sessions; every asynchronous payload
is checked against the selected `project_id`, so a late response from the
previous project is discarded. The id is routing and display state, not
authentication or cross-root security.

The loopback story is unchanged and non-negotiable: no auth, no
remote-bind flag on any spelling — SSH-tunnel the port if you need it
from another machine.

The Team Console lead-chat panel lets the local operator send a direct
message to the configured lead from the authenticated dashboard request
only. That path is gated by loopback, the dashboard session, and CSRF,
then records the operator identity as an auditable bus assertion. It is
not a cryptographic boundary against a fully privileged local process
that can write raw message files or inspect process memory; the generic
intent queue cannot authorize an operator-sender message.

For lead-chat reachability, the operator-facing lead must be actively
listening in an `agenttalk wait` or listen loop, wrapped, or refreshed by
the interactive activity hook so its heartbeat stays fresh. The
interactive hook is a heartbeat path, not a staleness exemption: when the
heartbeat is stale or missing, lead-chat still correctly reports the lead
as away instead of queueing into an unreachable listener.

The **Learning** panel is read-only. It shows accepted active lessons by
default, with the lesson text, trigger, publisher, curator, owner, evidence,
anchor metadata, exposure count, and recent wrapper exposure pointers. It
labels exposure as "surfaced" rather than "applied": the wrapper can prove an
accepted lesson was matched and handed to a child prompt, but only later review,
tests, or explicit evidence can prove the outcome followed it.

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

- **One consumer per agent.** Each agent is meant to run in exactly one
  consuming window per store. Cooperating cursor and threadstate
  read-modify-write paths are now cross-process serialized, so the old
  lost-update limitation is superseded. Duplicate consumers remain
  **unsupported** because they can both execute the same inbound work and
  produce conflicting replies before either cursor advances. 0.18.0 *warns* when
  `agenttalk wait` detects another live process already waiting as the same
  agent (advisory, best-effort — it never blocks and never changes the exit
  code), and `agenttalk doctor` reports the current waiter's PID. It does
  **not** enforce single-writer locking — the warning is a guardrail, not a
  guarantee. Pass `--refuse-stacked-wait` to turn that warning into a hard
  **exit 6** (refuse to arm a duplicate loop); a confirmed-dead waiter's
  ghost marker is reaped at arm, and `wait` also warns when more than 8
  live waiters share one store (leftover loops from old sessions). If a newer
  scoped wait for the same request arms, the older scoped wait exits **6** with
  `superseded` on stderr rather than later reporting a misleading timeout.
  Wrapper-owned waiting markers also carry a unique generation token; an old
  wrapper clears its marker only if the token still matches, so teardown cannot
  erase a replacement wrapper's marker.
- **Idle waiters back off.** `agenttalk wait` adaptively grows its poll
  interval from `--interval` up to `--max-poll-interval` (default 2.0s)
  while the bus is quiet, resetting to `--interval` the instant a message,
  composing, or rescind lands — so an idle bus polls near-zero without
  delaying a live reply by more than the cap. Set `--max-poll-interval`
  `<=` `--interval` to disable (fixed-interval polling).
- **Compaction bounds growth, and is lossy-by-design for *closed* history.**
  `agenttalk compact` moves a safe prefix of old messages into the cold
  `archived/compacted/` dir, which is **never read back** (same contract as
  `reset --archive`). It is engineered to be safe for everything *live* —
  it never archives a message unread by an active recipient, the epoch
  barrier, any protected (non-closed) thread's messages, or invalid files,
  so `current_epoch`, `threads`/`sync`, `wait`-on-rescind, and delivery are
  byte-for-byte unchanged across a compaction. The deliberate trade-off is
  the **retention boundary**: once an old *closed/resolved* request's
  messages are cold-archived, that request is no longer derivable, so a
  later `check --to-request <old-closed-id>` can return **unknown (exit 4)**
  rather than reconstructing its historical verdict. This is safe and
  fail-closed (unknown, never a wrong answer), but it is *not* byte-identical
  history — compaction trades cold closed-thread history for a bounded live
  store. Automatic compaction at `wait`-arm is **off by default**
  (`compact.enabled=false`); when enabled it only fires above
  `compact.trigger_threshold` live messages and is throttled by
  `compact.min_interval_seconds`. Tune retention with `compact.keep_count`
  (default 1000) and `compact.keep_age_days` (default 30); the manual
  `agenttalk compact` command runs regardless of the enable flag.
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
| `6` | `agenttalk wait` duplicate-wait class: `--refuse-stacked-wait` refused to stack on a live mailbox owner, or an older scoped wait was superseded by a newer same-thread waiter. Superseded waits print a stderr diagnostic and do not consume messages, advance cursors, or mark the thread seen. |
| `130` | `SIGINT` (Ctrl-C). |

---


0.14.0 additions: **3** = the request was superseded/rescinded
(`check`, and a scoped `wait --to-request` waking on a rescind);
**4** = unknown request id (`check`); **5** = PARTIAL broadcast fan-out (0.15.0 — some copies written, some failed; see the delivered/missed manifest); **6** = `wait` duplicate-wait class (`--refuse-stacked-wait` or superseded same-thread waiter). Exit 1 remains *exclusively* the
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
- **Message publication avoids append contention.** One JSON file per message
  is prepared and atomically published under the cooperating store lock.
  Append-only ledgers are separate JSONL surfaces: their owning modules
  serialize appends, fsync complete records, and readers isolate a malformed
  physical line without hiding later valid events.
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
