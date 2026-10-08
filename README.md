# agenttalk

**In plain words:** agenttalk lets you run a team of AI coding assistants, such as
Claude Code and Codex, on one project. They hand work to each other and review it
across AI companies; on Windows, an optional supervisor keeps them running; and a
shared, curated memory brings lessons from past work back into later turns. The
messages and the team's shared records are files in your project, so the messaging
needs no server and no account; a few things, such as logs and signing keys, live in
per-user folders on the same computer. This front page is for anyone deciding whether
agenttalk suits them, including readers who have never programmed, and the detailed
guide and reference follow further down.

**On this front page**
- [Who it is for, and what you need first](#who-it-is-for-and-what-you-need-first)
- [A normal day with a small team](#a-normal-day-with-a-small-team)
- [Honest limits](#honest-limits)
- [Get started](#get-started)

**The detailed guide and reference**
1. [How it works](#1-how-it-works), including [what it does well that you might not
   guess](#what-it-does-well-that-you-might-not-guess), [what stays on your
   machine](#what-stays-on-your-machine) and [what agenttalk is
   not](#what-agenttalk-is-not)
2. [Quick setup](#2-quick-setup)
3. [Use cases](#3-use-cases)
4. [In depth: how a migration works](#4-in-depth-how-a-migration-works)
5. [Technical reference and FAQ](#5-technical-reference-and-faq)

---

## Who it is for, and what you need first

agenttalk is for people who already use an AI coding assistant on real code and want
more than one: a second assistant, ideally from another AI company, to review or carry
on the first one's work, or a small team in which a lead hands out the work. It suits
one developer with two assistants as well as an operator who keeps a whole team
running all day.

What you need:
- **At least one AI coding assistant that runs in a terminal** (the text window where
  you type commands): Claude Code, Codex, or both. Each needs working access to its AI
  model: often a subscription, or an account the AI company bills by how much you use
  it; either way, that cost is yours. agenttalk does not supply AI models or pay for
  them. Its optional managed gateway can give a wrapped Claude Code seat (one that
  `agenttalk wrap` runs, turn by turn) one other model (Qwen, through OVH AI Endpoints,
  with your own key). To have work reviewed across AI
  companies, you need assistants from two companies.
- **Python 3.10 or newer**, to install agenttalk.
- **Windows, Linux or macOS.** The project's tests run on all three, with Python 3.10
  to 3.13. Some optional parts are tied to one system:
  - The bundled supervisor, the monitor that keeps an unattended team running, needs
    Windows and PowerShell 7. A monitor that restarts seats on Linux and macOS is not
    built yet; [#356](https://github.com/zoolok17/agenttalk/issues/356) tracks it.
  - The optional managed gateway runs as a background service through Windows Task
    Scheduler, or through a systemd user service on other systems. macOS has no
    systemd, so that service cannot be installed on a Mac.

  On Linux and macOS, a wrapped Codex seat whose shell (the program that reads the
  commands you type) is bash or zsh needs one setting, `"reply_shell": "bash"` in
  `.agenttalk/supervisor.json`, because the wrapper gives Codex its reply instructions
  in PowerShell form by default. A Codex assistant you start yourself does not use
  this setting.
- **git**, because reviews, deliveries and release checks name exact versions of the
  code (commits).

**Does setup need technical help?** A pair of assistants needs a terminal. You run a
few commands to install agenttalk and set up the project, set one setting (an
environment variable) in each terminal, and start the assistants; [Get
started](#get-started) shows them. If that is new to you, ask someone who has done it
before. An unattended team, with the supervisor and a lead woken on a schedule, is a
bigger job for someone technical; [the supervisor tutorial](docs/supervisor-tutorial.md)
walks through it.

## A normal day with a small team

Picture three seats. A **seat** is one AI assistant with its own name on the team; in
the simplest setup, each runs in its own terminal. Here they are a lead, a builder called `claude-dev` (a Claude Code
seat) and a reviewer called `codex-rev` (a Codex seat). The lead can be you, or an AI
assistant that talks to you.

1. **The lead hands out work.** It writes a work order that stands on its own: what to
   build, how to check it, and what not to touch. An assistant may still be busy with
   its current job when the order arrives, and may remember nothing of earlier
   conversations, so everything it needs goes into the order.
2. **One assistant builds.** `claude-dev` does the work on its own branch, runs the
   checks and replies that it is done, naming the exact commit it built.
3. **Another assistant, from a different AI company, checks it.** The lead sends that
   commit to `codex-rev`. The reviewer reads it without the builder's reasoning, tries
   to break it, and replies with GO (go ahead) or with findings. Findings go back to
   the builder for a fix, and the reviewer reads the new commit.
4. **A person decides anything risky: merging, releasing, deleting.** agenttalk itself
   does not merge code or make releases. A person does, or a lead that person has
   allowed to, and the lead's own instructions say to merge only on a GO for that exact
   commit. Before such a step, the assistant should run agenttalk's pre-action check
   with the gates option:
   `agenttalk check --for <seat> --to-request <request-id> --gates`. It says stop if
   the request was withdrawn, or if a gate still says HOLD; a gate is a named check the
   team records as HOLD (not yet) or GO. Without `--gates` the check does not look at
   the gates, and the bundled lead instructions show it without that option today
   ([#362](https://github.com/zoolok17/agenttalk/issues/362)). Sending a newer request
   in place of an old one does not stop the old one: to stop it, withdraw it
   (`agenttalk rescind`). A question only a person can answer goes to them with
   `agenttalk escalate`.
5. **It is written down.** Work orders, replies and reviews are messages, kept as
   files in the project's `.agenttalk/` folder, and the team's lessons are kept there
   too. When an assistant forgets, crashes or restarts, it can find its open work
   again in those records rather than in its memory, and you can look up who asked
   what and who answered.

## Honest limits

- **It coordinates; it does not add capacity.** The team works on one machine and
  within the usage limits of your AI accounts. When a company's usage limit runs out,
  the seats on that account cannot work, sometimes for hours. A wrapped assistant
  handles one message per turn. When one lead hands out and checks all the work, as in
  the normal day above, that lead becomes the bottleneck as the team grows. A separate service that keeps track of AI usage across
  subscriptions and pay-per-use accounts, and shares it out between teams, is being
  developed alongside agenttalk, and agenttalk will be its first user. The service is
  not released yet.
- **More assistants do not mean correct work.** A person still writes clear work
  orders, insists on review before anything merges, and keeps an eye on disk space;
  agenttalk does not enforce any of these.
- **Delivered, done and recorded are three different things.** A message can be
  delivered and read while the work is still running. The work can be finished while
  the reply that says so has not been sent yet. A work order closes when its reply
  says it is done or declined. When the reply has no status field, a verdict closes it
  too. A reply that only accepts the work keeps it open. A seat can also close it for
  itself by hand, with `agenttalk ack --for <seat> --to-request <request-id>`, and
  withdrawing it (`agenttalk rescind`) closes it for everyone. So closed does not
  always mean completed: read the reply. `agenttalk threads` lists what is still open,
  and a quiet screen does not mean nothing is outstanding. A withdrawn request stays
  closed, even if an answer arrives later, but withdrawing it does not stop an
  assistant that is already working on it.
- **Known gaps today.**
  - **Lanes do not work when the bus lives outside the code.** Lanes are the delivery
    check for a scoped piece of work. They need the bus folder inside the code
    repository; otherwise they cannot find the branch to start from, and shared notes
    tied to a file path are marked out of date at once
    ([#245](https://github.com/zoolok17/agenttalk/issues/245)).
  - **Temporary files still land in the shared temp folder.** The wrapper tells each
    wrapped assistant to keep its temporary files in its own scratch folder, but
    agenttalk does not yet point the programs they run there; by default those files still go to the user's
    temp folder ([#336](https://github.com/zoolok17/agenttalk/issues/336)).
  - **An unattended seat may fail to reply.** The reply instructions the wrapper gives
    a seat leave out the seat's name, and the wrapper does not pass the name on. Unless
    the name reaches the seat another way, its reply stops with "no agent identity".
    For an ordinary seat, the supervisor's settings file can carry the name
    (`AGENTTALK_SELF` in the seat's `env` entry, as the starter settings file
    that `agenttalk supervise --init` writes already does). A seat that uses the
    optional managed gateway cannot use that setting, so it must name itself in the
    reply instead, with `--from <seat>` ([#354](https://github.com/zoolok17/agenttalk/issues/354); the
    wider problem of making sure a reply lands is
    [#178](https://github.com/zoolok17/agenttalk/issues/178)).

## Get started

Install a released version and the instructions each assistant reads:

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.98.0"
agenttalk install-skills
```

Then, in your project's top folder:

1. Name the seats: `agenttalk init --here --agents claude-dev,codex-rev`.
2. In each terminal, set that seat's name before you start the assistant, for example
   `$env:AGENTTALK_SELF = 'claude-dev'` in PowerShell, or
   `export AGENTTALK_SELF=claude-dev` in bash or zsh.
3. For Codex, read what `agenttalk codex-config --enable` allows, then run it.
4. Start one assistant and tell it to add itself as the lead. Start the other and tell
   it to join as a reviewer and wait for the lead.
5. Ask the lead to hand a piece of work to the other assistant for review.

This two-terminal pair is the simplest first try. For a bigger team you still start
only one assistant, the lead. The lead can add the teammates to the roster and write
the supervisor's settings. On Windows with PowerShell 7 you then start the supervisor
yourself ([#355](https://github.com/zoolok17/agenttalk/issues/355) would let the lead
take that step too), and the supervisor, not the lead, starts the teammates in its
settings in the background, by default with no window of their own. [Start one agent
as a self-guiding lead](#start-one-agent-as-a-self-guiding-lead) explains how.

[Quick setup](#2-quick-setup) explains each step. After that, [Use
cases](#3-use-cases) shows the shapes a team grows into,
[the migration method](#4-in-depth-how-a-migration-works) describes the flagship use,
and [the technical reference](#5-technical-reference-and-faq) has the command tables
and the FAQ. For a concept-first introduction, read [the new-user
manual](docs/AGENTTALK-NEW-USER-MANUAL.md).

---

## 1. How it works

agenttalk is a small message bus: a shared place where agents leave
messages for each other. You install it with one `pip` command, set up the
project once, give each terminal an agent name, and the agents can then hand
work to each other and reply; everything else, from team roles to a read-only
web dashboard, is optional. Coding agents that run as command-line tools
(CLIs), such as Claude Code and Codex, use it to talk to each other
directly and work on the same repository, as a pair or as a named team.
The messaging needs no background service and no server: every message
is a JSON file under a project-local `.agenttalk/` directory. Each CLI
runs in its own terminal window, so you can watch the whole conversation
as it happens.

### Why a bus instead of copy-paste

The default way to get a second opinion from another agent is to copy
a diff out of one chat window and paste it into another, then copy the
review back. agenttalk removes the copy-paste: one agent sends a
message, the other wakes up, does the work, and replies. You stay in
the loop the whole time and can interrupt either side whenever you
want.

### Vendor diversity is a review property, not a preference

agenttalk treats "which vendor implemented this" and "which vendor
reviews it" as independent choices. Claude-implements-Codex-reviews
and Codex-implements-Claude-reviews are equally supported, and nothing
about the bus favors either direction. The reason to pair different
model vendors, rather than running two instances of the same one, is
that a reviewer with a different training lineage (a model trained by a
different company) is less likely to share the first agent's blind spots. The bus is symmetric; which agent
plays which part on a given task is your call.

### What it does well that you might not guess

- **Review across AI companies catches real bugs.** An assistant from a different
  company has different blind spots. In this project's own development, such reviews
  regularly find real bugs in work that its author's checks had passed. In this
  project's own reviews, hundreds of passing tests still missed concrete problems that
  a careful reviewer found.
- **A lead insists on a fresh GO for the exact commit.** When a lead follows
  agenttalk's lead instructions, it merges only on a GO that names the exact commit
  being merged; a GO for an earlier commit does not count, so a later change needs a
  new review. agenttalk does not enforce this for every review: a quick review need
  not name a commit, and agenttalk cannot stop a person's own merge command. The release
  check (`agenttalk dev-gate`) also ties its evidence to the commit it tested.
- **A restarted assistant can carry on.** After a restart, the wrapper
  (`agenttalk wrap`) tries to resume the assistant's own session. When that session
  cannot be used, for example because it is missing or damaged, or after a change of
  model, it starts a fresh session instead. Either way the assistant's open requests
  are on the bus. A checkpoint (`agenttalk checkpoint save`, run by the assistant or a
  hook) also records what it was working on before its conversation history gets
  trimmed to make room: its open threads, its git state and how full its memory was.
  That is not its whole conversation, only what it needs to find its place.
- **Lessons come back as reminders.** When the team learns something the hard way, an
  assistant publishes it as a lesson in a shared store (`agenttalk knowledge`). Once a
  curator, a person or seat trusted to review lessons, has accepted it, it can come
  back by itself: a wrapped seat (one the wrapper runs, turn by turn) gets up to five
  selected lessons that match its task, and a seat run by hand sees them through
  `agenttalk sync`. They are reminders;
  they make it less likely that the next assistant repeats the mistake, but they do not
  prevent it.

### What grows around that core

The two-agent handoff is the whole essence, and it stays simple: one agent
hands work to another and waits for the answer (`send`/`reply`, or the
`/agenttalk.handoff` skill). Everything past that is optional, added as a
pair grows into a team or starts running unattended: named teams and roles,
safety checks before anything risky, a supervisor that restarts crashed
assistants, a register of who owns which part of the code, a shared memory
for lessons, and a read-only dashboard for watching it all.
[Optional features at a glance](#optional-features-at-a-glance) in the
technical reference describes each one, and the command tables there list the
actual commands.

### Local-first: what reaches the network

The bus itself makes no network calls: messages are files on disk. Your
agent CLIs (Claude Code, Codex) keep talking to their own model providers,
as they would without agenttalk. These are agenttalk's built-in network
integrations, and when they connect:

- **DEFAULT: none.** The bus, the dashboard and the supervisor open no
  outbound connections.
- **OPT-IN, the managed model gateway** (`agenttalk gateway`): while it is
  installed and running, it sends gateway-backed agents' model calls to
  OVH AI Endpoints at `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1`.
- **OPT-IN, the contributor check `agenttalk dev-gate`** (this project's
  CI runs it too): when you run it, it installs the package it built
  locally, fetching that package's dependencies and the test tools from
  the Python package index (`https://pypi.org/simple`), downloads the live
  Semgrep rule sets `p/python` and `p/security-audit`
  from the Semgrep registry, and checks dependencies against the PyPI
  advisory database through `pip-audit`.
- **OPT-IN, the assurance tool** (`python -m agenttalk.assurance`): the
  OSV vulnerability database (through `osv-scanner`), the PyPI advisory
  database (through `pip-audit`) and remote Semgrep configurations are all
  off by default; each runs only when the scan profile's `network_allowed`
  setting lists that tool. A `release`-profile scan also builds the
  package with `python -m build`. That build is not gated by
  `network_allowed`: it can download the project's build backend from the
  Python package index even when `network_allowed` allows nothing.

Commands you configure yourself, such as the assurance tool's test,
coverage and tool commands or a project's build backend, can reach
anything, and `network_allowed` is not an operating-system network
sandbox.

The bundled dashboard binds to loopback only (it listens only on this
computer) and has no flag to expose it — reach it from another machine
over an SSH tunnel if you need to, not by opening the port. The dashboard also answers
requests addressed to a loopback address (for example `127.0.0.1`, `localhost` or `[::1]`) with the
dashboard's own port; an omitted port means 80. Anything else gets a 403. A request with no `Host` header,
with more than one, or with one that is not exactly a host and an optional port gets a 400, and so does a
malformed full-URL request target. A full-URL target (`GET http://host:port/...`) is checked the same way for
every request type. This stops a web page from reading the dashboard through DNS rebinding. With an SSH
tunnel, use the same port number on both ends (`-L 8765:127.0.0.1:8765`).

Where agenttalk keeps its files, including the few it keeps outside your
project, is listed under [Where agenttalk keeps files](#where-agenttalk-keeps-files)
in the technical reference.

### What stays on your machine

- **The team's records are files in your project.** Messages, the team roster, open
  requests, decisions and lessons live in the project's `.agenttalk/` folder. A few
  things live in per-user folders on the same computer: signing keys, backups,
  supervisor logs, the optional turn journal and the optional gateway's files, and
  `agenttalk codex-config --enable` adds a block to Codex's own settings file.
  [Where agenttalk keeps files](#where-agenttalk-keeps-files) lists them, which matters
  when you back up or move a project.
- **agenttalk itself sends nothing over the network by default.** Your AI assistants
  keep talking to their own companies, as they would without agenttalk. Three optional
  parts use the network when you turn them on: the managed model gateway, the project's
  own build-and-test check and the assurance scanner. [What reaches the
  network](#local-first-what-reaches-the-network) says exactly what each one contacts.
- **No server and no account, but an unattended team needs more than the bus.** The
  messages need nothing running in the background. A team that keeps working while you
  are away also needs:
  - the wrapper (`agenttalk wrap --loop`), which hands each assistant its messages one
    turn at a time and tries to keep its session;
  - the supervisor, which starts the assistants and restarts them after a crash or an
    outage. It runs on Windows only today; [#356](https://github.com/zoolok17/agenttalk/issues/356) tracks a monitor for Linux and
    macOS, and [the supervisor tutorial](docs/supervisor-tutorial.md) explains how to
    start it;
  - something that wakes the lead on a schedule, such as the managed lead loop's regular
    check-in (`agenttalk wrap --loop --lead-loop --for <agent>`, once that identity is
    registered with `agenttalk managed-lead-loop set <agent>`; [the agent
    manual](docs/AGENT-MANUAL.md#5-the-v0420-split-identity-lead-loop) explains both steps) or a scheduled job of your own.

### What agenttalk is not

- **Not a model.** The bus doesn't choose or run a model and has no
  opinion on which model a CLI uses — it moves messages between whatever
  agent CLIs (the assistants' command-line programs) you start. The intelligence is entirely in the agents; the
  bus just lets them talk. The one model-specific part is the optional
  managed gateway, which supports a single route, pinned in code: OVH AI
  Endpoints with the Qwen3.8-27B model. It refuses any other address or
  model.
- **Not an IDE plugin** (an add-on for a code editor). There's no editor
  integration to install. agenttalk is a CLI-level bus, used from the
  command line: it works with whatever terminal or editor-embedded terminal
  you already run your agent CLIs in.
- **Not a hosted service.** No account, no server to sign up for, no
  cloud component. agenttalk's own files stay on your own machine: the
  coordination store in your project's `.agenttalk/` directory, and the
  per-user files listed under [Where agenttalk keeps
  files](#where-agenttalk-keeps-files).
- **Not a task queue.** There's no central scheduler deciding what
  runs next; agents decide what to do and message each other about it.
  If you want an explicit "what's next" driver, pair agenttalk with a
  planning/work-breakdown tool of your choice — agenttalk carries the
  wake signal, the planning tool remains the source of truth for state.
- **Not a replacement for git.** No agenttalk command merges work into
  your main branch; that merge is done outside agenttalk, with git or your
  code host. Some optional features do create branches or commits:
  - **Lanes.** By default, `lane assign` creates a branch and a working
    folder (a git worktree) for one piece of work; an advisory lane assigned
    with `--advisory --no-worktree --worktree-waiver-reason <reason>` creates
    neither. `lane abandon` ordinarily removes that
    folder if it is clean and idle, and keeps the branch unless you add
    `--delete-branch` and git confirms the branch's work is already in the
    target. `lane gc --delete` is a separate cleanup with its own checks.
  - **The cleanup tool's apply mode** (`agenttalk janitor --apply`). It can
    stage every change in a worktree, new files included, and save it as a
    commit on that worktree's branch, to preserve the work before cleaning up.

  `lane` and `domain` gate *who may deliver what*, using git diffs as
  evidence; they don't perform the merge.
- **Not a multi-machine system.** Both agents are expected to share one
  project directory on one machine (or a directory synced by a
  mechanism you already trust). There's no transport, no server
  process, and no attempt to solve distributed consensus (keeping copies on
  several computers in agreement).

---

## 2. Quick setup

### Install (tag-pinned)

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.98.0"
agenttalk install-skills          # installs bus skills + the dev-discipline devkit
```

The first line installs agenttalk itself. Pin it to a released tag
(`@v0.98.0` above, or whatever the current release is) rather than a
branch: the commands and the message format can change between
releases, and a tag keeps every agent in a project on the same version.

The second line installs the skills, the instructions each agent CLI
reads to use agenttalk. It writes Claude Code's bus commands under
`~/.claude/commands` and Codex's under `~/.codex/skills`, and the
dev-discipline pack (the devkit) under both `~/.claude/skills` and
`~/.codex/skills`. `--claude-only` and `--codex-only` choose which
side gets the bus commands; the devkit still goes to both sides unless
you add `--no-devkit`, so `--claude-only --no-devkit` installs Claude
Code's bus commands and nothing else. `--devkit-only` installs only the
devkit, and `--dry-run` previews without writing.

When you upgrade an existing install, a plain `agenttalk install-skills`
leaves alone every skill file that differs from the new version, so
updated skills would not arrive. Preview with
`agenttalk install-skills --dry-run --force`, back up any local edits you
want to keep, then run `agenttalk install-skills --force`.

### Initialize a project

Do this once per project, from the project's top folder. Name the agents
you will run, one per terminal; use real names rather than the default
`claude` and `codex` (the next section explains why):

```powershell
agenttalk init --here --agents claude-dev,codex-rev
agenttalk codex-config --enable   # see "Let Codex call agenttalk" below
```

`init --here` is shorthand for `init --path .`; it writes
`.agenttalk/config.json` with the roster you named.

A one-window project is valid too: initialize with a single agent name
and add peers later with `agenttalk roster add`.

### Give each terminal its identity

Every `agenttalk` command needs to know which agent it's running as.
Set `AGENTTALK_SELF` in each terminal **before** starting the CLI, so
every bus command that CLI runs (directly or through a skill) resolves
to the right identity automatically:

```powershell
# Terminal A (PowerShell)
$env:AGENTTALK_SELF = 'claude-dev'
claude

# Terminal B (PowerShell)
$env:AGENTTALK_SELF = 'codex-rev'
codex
```

```bash
# Terminal A (bash)
export AGENTTALK_SELF=claude-dev
claude

# Terminal B (bash)
export AGENTTALK_SELF=codex-rev
codex
```

This matters even for the self-guided flow below: an agent that "picks
its own name" still needs `AGENTTALK_SELF` set in its terminal (or an
explicit `--from <name>` on every bus command it runs). Without either, a
bus command run directly stops with "no agent identity" (exit code 2).
The bundled skills behave differently: when `AGENTTALK_SELF` is unset they
use a default name, `claude` in Claude Code and `codex` in Codex. If the
roster has an agent of that name, the skill's messages go to or from that
agent with no error. `agenttalk init` prints this same reminder after it
runs; don't skip it.

### Let Codex call agenttalk

`agenttalk codex-config --enable` writes a per-project block into
Codex's own `~/.codex/config.toml`:

```toml
trust_level = "trusted"
approval_policy = "never"
sandbox_mode = "workspace-write"
```

This is broader than just agenttalk: it removes Codex's approval
prompts for **every** command it runs in this project, not only bus
commands, and grants workspace-write sandbox access. Without it, Codex
prompts for approval on each `agenttalk` invocation, which breaks
unattended listen loops. Consent to this knowingly — check the current
state with `agenttalk codex-config --status`, and undo it with
`agenttalk codex-config --disable` (this keeps `trust_level`, only
clearing `approval_policy`/`sandbox_mode`).

### Start one agent as a self-guiding lead

You don't have to pre-declare a roster or learn commands to get the
first agent onto the bus. Start a fresh CLI — with `AGENTTALK_SELF` set
in that terminal, as above — in the project root and tell it, in plain
language:

> We use agenttalk in this project. Add yourself as the human-facing
> lead, then check whether the team is up and running.

The agent reads its bus skill, picks a name (matching the
`AGENTTALK_SELF` you set, or its own choice if you left it unset —
which the identity note above explains why to avoid), runs `agenttalk
roster add <name> --role lead` and `agenttalk roster set-operator-facing
<name>` (so it becomes the one agent you talk to, and the one
escalations route to), then runs `agenttalk roster` / `agenttalk
status` / `agenttalk sync` to see who else is online. From there it
coordinates the rest of the team on your behalf.

**You do not have to set up each teammate by hand.** You start one
assistant, the lead, and ask it to set up the team. It adds each teammate
to the roster (`agenttalk roster add`) and writes the supervisor's settings
(`agenttalk supervise --init` writes a starting `.agenttalk/supervisor.json`),
with each teammate run through the wrapper.

You then start the supervisor yourself, with the PowerShell command in [the
supervisor tutorial](docs/supervisor-tutorial.md): the lead's instructions today tell it
never to start other assistants itself, and [#355](https://github.com/zoolok17/agenttalk/issues/355) would let it take this
step too. The supervisor starts the teammates in its settings in the
background, by default with no window of their own, and you watch the team in the read-only dashboard,
`agenttalk dashboard`, which runs only on your own computer. This works on
Windows with PowerShell 7 today; on Linux and macOS you start each teammate
yourself ([#356](https://github.com/zoolok17/agenttalk/issues/356)). The two-terminal pair in the next section stays the
simplest first try.

### Add a second agent of another vendor

Open a second CLI — from the other vendor, so the two agents don't
share a training lineage, with `AGENTTALK_SELF` set in that terminal —
in the same project root and tell it:

> We use agenttalk in this project. Add yourself to the roster as a
> developer using the name already in your AGENTTALK_SELF, then wait
> for the lead to contact you.

It runs `agenttalk roster add <name> --role developer`, then drops into
listen mode: `/agenttalk.listen` on Claude, `$agenttalk-listen` on
Codex. **Roles are free-form labels** — `developer`, `reviewer`,
`tester`, or anything else you name works; the CLI doesn't special-case
any particular string.

If you'd rather wire this up yourself instead of talking the agents
through it:

```powershell
agenttalk roster add claude-dev --role developer
agenttalk roster add codex-rev --role reviewer
agenttalk roster
```

If a CLI was already open when you ran `agenttalk install-skills`,
restart it — skills are loaded at startup, so an already-running
session won't see newly installed ones.

### First review round

With both agents up, ask the lead (or either agent directly) to hand
off a piece of work and block on the reply. On Claude:

> Implement <task>, then use /agenttalk.handoff to send it to
> <the other agent> for review.

On Codex, the equivalent skill is hyphenated: `$agenttalk-handoff`.
Both wrap the same underlying commands — Claude's dotted names
(`agenttalk.send`, `agenttalk.listen`, `agenttalk.handoff`, ...) and
Codex's hyphenated names (`agenttalk-send`, `agenttalk-listen`,
`agenttalk-handoff`, ...) are two spellings of identical behavior.

Under the hood this is one `agenttalk send --kind review-request` (or
the handoff skill, which wraps `send` and then waits) from the
implementer, a wake for the reviewer's listen loop, and an `agenttalk
reply` back. Watch both terminals — each shows the full exchange as it
happens, since `send` prints to the sender's stdout and the receiver's
`wait` prints the same message on the other side.

To check in on a rejoined or idle session without re-explaining
context, run:

```powershell
agenttalk roster
agenttalk status
agenttalk sync --for <agent>
```

`sync` summarizes roster identity, open request threads, unread
traffic, and deterministic next-action hints — it's the fastest way
for an agent (or you) to answer "what's outstanding right now."

### What's next

This covers a working pair with one review round. For fan-out to a
whole team, unattended 24/7 operation, shared ownership over repo
areas, and the full command reference, see [Use
cases](#3-use-cases) and the [Technical
reference](#5-technical-reference-and-faq).

---

## 3. Use cases

Four shapes cover most projects that adopt agenttalk. Each is a
starting point, not a fixed track — teams mix and match as the work
changes.

### Ad hoc: a quick implement-then-review loop

The smallest useful shape: one agent implements, another reviews, no
pre-declared roster. Tell a fresh CLI the [minimal-start
instructions](#start-one-agent-as-a-self-guiding-lead), or
wire it up directly:

```powershell
agenttalk roster add claude-dev --role developer
agenttalk roster add codex-rev --role reviewer
```

Then hand off with `/agenttalk.handoff` (send + block on reply) or
`/agenttalk.consult` (confer with the peer before answering you), and
let the reviewer's `/agenttalk.listen` loop pick it up. This is the
right shape for a single feature, a bug fix, or any task where you
don't need named teams, gates, or unattended operation — just a fast,
observable second opinion from a different model vendor.

### Migrating an old codebase (the flagship use case)

agenttalk was built for exactly this: a legacy codebase nobody fully
trusts yet, worked by more than one agent over more than one session,
where the risk isn't writing code — it's writing code against a wrong
belief about what the old code does.

Three features compose for this:

- **`agenttalk onboarding`** is a durable ledger for the first pass:
  agents record which segments of the codebase they read, claims they
  believe (with evidence pointers), drift between docs and actual
  behavior, and open unknowns — optionally marked `--blocking` so a
  migration step can't quietly proceed past an unanswered question.
  This is evidence capture, not an analyzer: it doesn't decide what's
  true, it makes sure disagreements and gaps are visible instead of
  silently assumed away.
- **`agenttalk domain`** turns "who owns this part of the legacy tree"
  into a checkable registry instead of tribal knowledge, so a second
  agent joining mid-migration can ask `domain check-path` instead of
  guessing.
- **`agenttalk lane`** gates *delivering* a change against that
  registry: a lane is a scoped assignment (a domain subset, a base
  SHA, a target ref), and `lane check` computes the actual diff,
  checks it against domain bounds and other active lanes, and runs a
  real merge check — HOLD or GO, never an inferred "probably clean."
  Today lanes need the bus folder inside the code repository: with the
  bus elsewhere, `lane assign` cannot resolve the branch to start from,
  and knowledge notes anchored to a file path are marked stale at once
  ([#245](https://github.com/zoolok17/agenttalk/issues/245)).

A fourth command, **`agenttalk comprehension`**, gives a migration
surface accounting instead of a guess: `comprehension scan` builds a
local, offline, immutable inventory of the repository by feature and
unit, and `comprehension report`/`status` query one run at a time.
Feature and unit ids are deterministic hashes over each item's kind,
path, and qualified name — not run-specific — so **rescanning the same
paths** is directly comparable: an unchanged file re-derives the same
id, and the diff between two runs' id sets shows which ids appeared or
disappeared — not what changed inside them, since no content goes into
an id, only kind/path/name. A file move, a package or class rename, or
a framework change re-keys every id derived from it, so a real stack
migration's diff reads as removals and additions you pair up yourself,
not silent reappearance — explain each one, the same way section 4
already treats a rename or split (see
[§4](#4-in-depth-how-a-migration-works)). Set equality is surface
accounting, not behavioral evidence; it says nothing about whether a
feature still works. (A built-in cross-run comparison command is not
yet shipped — you diff the two runs' id sets yourself today.)

Together, this gives a migration a paper trail: what was read before
it was changed, who owns the part being touched, a deliver gate that
composes with `close`/`gate` review evidence instead of trusting a
change is safe because nobody objected, and a surface-level accounting
of what moved, was added, or was removed. See [Technical
reference](#5-technical-reference-and-faq) for the full command
tables, and [§4](#4-in-depth-how-a-migration-works) for the method in
full.

### Greenfield: spec, plan, build, with review from round one

For a new project, there's no legacy-belief risk to guard against, so
the emphasis shifts to keeping a growing team's roles straight from
the start, and getting review into the loop before the first feature
lands rather than after. Wire up roles directly:

```powershell
agenttalk init --here --agents claude-dev,codex-dev,claude-rev,codex-rev,claude-lead
agenttalk roster set-role claude-dev implementer
agenttalk roster set-role codex-rev reviewer
agenttalk roster set-group devs claude-dev,codex-dev
agenttalk roster set-group reviewers claude-rev,codex-rev
agenttalk roster --json
```

Route implementation handoffs to a reviewer that didn't write the
code (fresh review), and use `agenttalk broadcast --to-group reviewers
--kind question` for anything the whole review group should weigh in
on. A `domains.json` is worth authoring early here too — it's cheap to
write against a codebase you're still designing, and it means the
`lane` deliver-gate is available from the first PR instead of being
retrofitted later.

### Operating an existing project day to day

Once a project is past its initial migration or greenfield push, the
steady-state pattern is mostly `agenttalk sync` and `agenttalk status`
at the start of a session, ordinary handoffs for day-to-day changes,
and `agenttalk gate` / `agenttalk close` around anything release-shaped.
For a project that needs to keep working unattended — overnight,
across an outage, or simply longer than you want to watch a terminal —
add the supervisor and the `agenttalk wrap` progress wrapper so agents
try to resume their session context instead of starting over; when a
session cannot be resumed, the wrapper starts a fresh one. The reply
instructions the wrapper gives a seat do not include `--from`, and the
wrapper does not pass the seat's name on, so a seat's reply stops with "no
agent identity" unless the name reaches it another way
([#354](https://github.com/zoolok17/agenttalk/issues/354); the wider
reply-receipt problem is
[#178](https://github.com/zoolok17/agenttalk/issues/178)). For an ordinary
seat, keep `AGENTTALK_SELF` in its `env` entry in `supervisor.json`, as the
scaffold from `agenttalk supervise --init` writes it. A gateway-backed
(`ovh-qwen`) seat refuses any literal per-agent `env`, so it must reply with
`--from <seat>`. A team that runs
while you are away also needs something that wakes the lead on a schedule,
such as the managed lead loop's regular check-in tick
(`agenttalk wrap --loop --lead-loop --for <agent>`, once that identity is
registered with `agenttalk managed-lead-loop set <agent>`; see the
[agent operating manual](docs/AGENT-MANUAL.md#5-the-v0420-split-identity-lead-loop)) or a scheduled job of your own.
See [docs/supervisor-tutorial.md](docs/supervisor-tutorial.md) for the
supervisor quick start, and [Technical
reference](#5-technical-reference-and-faq) for the assurance-gate
command tables.

---

## 4. In depth: how a migration works

This section is for a team planning a migration of an existing application.
It explains how to preserve behavior while changing the stack beneath it.

A migration needs more than a successful build on newer dependencies.
You need an account of what existed, what changed, and how each claim was checked.
Agenttalk helps the team share that account and coordinate independent work.
The team remains responsible for the measurements and the release decision.

### Start with a map of the application

Before changing a legacy application, freeze an identifiable baseline revision.
Capture the application surface while its original behavior is still available.
Keep the source revision, scan inputs, and extraction limitations with the map.

Three inventories answer different questions:

| Inventory | Question it helps answer |
|---|---|
| Features | Which user-visible capabilities must survive? |
| Entry points | Where can requests, jobs, messages, or other inputs enter? |
| Calls and dependencies | Which collaborators and data operations does the code rely on? |

The comprehension producer supplies a bounded static inventory for supported inputs.
It records candidate features, recognized entry points, and coarse dependencies.
It also exposes exclusions, unsupported shapes, and unresolved relationships.
The team supplements those results where the application exceeds the adapter's scope.

A feature inventory is not automatically a complete list of business capabilities.
A static call inventory is not a complete runtime call graph.
Scheduled work, framework binding, and indirect calls need particular care.
Read the [comprehension limitations](docs/COMPREHENSION-LIMITATIONS.md)
before treating an empty result as proof that a mechanism does not exist.

At each slice close, capture the integrated revision again.
Compare the feature and entry-point identities with the baseline identities.
Equal totals alone are insufficient: one removed feature can hide behind one addition.
Compare the actual sets and explain every addition, removal, rename, or split.

Byte-identical sorted identity lists establish equality of those lists.
They support a claim that the captured surface remains accounted for one-to-one.
They do not prove that each feature still behaves correctly.
Behavioral tests and independent review supply that separate evidence.

Keep comparisons meaningful by recording scope and extractor versions.
A changed parser can alter an inventory even when application code is unchanged.
Keep source-byte identity separate from normalized inventory identity.
Capture metadata can differ between two scans of the same source.

The team performs the cross-revision reconciliation as part of this method.
Automatic multi-run fact comparison and richer runtime joins are planned
but not yet tracked by a dedicated issue. The
[comprehension design](docs/DESIGN-55-comprehension-plane.md)
describes those boundaries; its proposed surfaces are not an implementation checklist.

The current Java extraction layer has documented parsing and binding limits.
Its parser replacement is planned in
[the extraction upgrade](https://github.com/zoolok17/agenttalk/issues/141).
A scan completing does not erase those limits or certify migration readiness.

### Measure a baseline before changing behavior

The baseline register connects the inventory to observations.
Each in-scope behavior gets a reproducible case before the implementation changes.
Include failure behavior, not only successful requests.

Useful cases cover responses, authorization decisions, persisted values,
session transitions, scheduled work, and interactions with external systems.
Choose the cases from the application's actual entry points and call sites.
Record what remains unmeasured instead of giving it an implied pass.

Each register entry identifies the behavior, revision, input, and expected result.
It links the observed result to retained evidence.
It also names the owner of any unresolved decision.

Keep original observations when later measurements supersede them.
An appended correction explains why the current interpretation changed.
Deleting the earlier result would hide the path by which the team learned it was wrong.

“Pre-existing” is a measured attribution, not an excuse or a guess from a diff.
Run the same probe at the migration base and the candidate revision.
If the mechanism is older, also run it at the original application baseline.
Quote the revision and result beside the attribution.

Positive controls matter when comparing different generations of a stack.
A login that fails because an old driver cannot query a new server
does not prove that the old authorization rules safely deny an attacker.
Record the incompatibility separately from the permission being tested.

When a compatible historical environment is required, identify that qualification.
When the older implementation lacks the mechanism, report the comparison unavailable.
A skipped case or a compilation failure is not a passing behavioral baseline.
An inherited defect still needs a fix or an explicit owner disposition.

### Turn guardrails into a living contract

Guardrails state the boundaries within which the migration may run.
They cover source identity, test isolation, data ownership, evidence, and cleanup.
They also identify actions reserved for the operator.

Give each rule a stable identifier, a reason, and a way to check it.
Link an incident to the rule that prevents its recurrence.
A rule without an observable check is easy to repeat and difficult to enforce.

Keep related rules together:

| Family | Evidence the team needs |
|---|---|
| Source identity | The exact revision and any temporary probe changes |
| Test sensitivity | A known fault makes the intended assertion fail |
| Environment isolation | Owned services, fresh data, and identified toolchains |
| External effects | Network boundaries and actions that remain prohibited |
| Artifact integrity | Fresh outputs and comparisons against recorded inputs |
| Cleanup | Owned processes stopped and temporary work accounted for |

The register carries project-specific measurements.
Reusable documentation explains the method without carrying client identities,
private machine details, account names, or internal work identifiers.
Generated structure can also be sensitive even when it contains no source text.

The [development methodology](docs/DEVELOPMENT-METHODOLOGY.md)
explains the reasoning behind mutation checks, cold reads, and retained limitations.
These practices require explicit execution records.
Describing a practice does not prove that a particular run followed it.

### Prove that the tests can detect the fault

For a defect fix, first demonstrate the failure with a focused behavioral case.
Apply the fix and demonstrate the expected result.
Then temporarily remove the mechanism that makes the fix work.

The associated assertion must fail for the intended reason.
A compilation error or an unrelated connection failure does not validate that assertion.
Restore the mechanism and confirm the case passes again.

Target the decision being protected, not merely a nearby line of code.
Removing a compare-and-set condition should expose a stale writer in its own case.
Two threads serialized by a shared lock cannot establish a multi-process guarantee.
The test must create the competing history that the mechanism is meant to reject.

Inspect the inputs that cross a test seam.
A fake database that ignores its query can keep passing after a filter is removed.
An assertion about an exit status can miss a false statement in the result payload.
Check the decision, its data, and the positive controls together.

Keep the mutation, command, observed failure, and restored result together.
That record lets another reviewer reproduce the claim without trusting its author.
Small deterministic cases make later fix rounds cheaper to verify.

### Build slices that can be closed independently

A slice is a bounded change with a defined acceptance bar.
Its steps have owners, dependencies, and concrete proof obligations.
Separate a database compatibility question from an application rewrite when possible.
Separate a source change from the deployment action that may eventually use it.

Authors work in isolated branches or workspaces.
Each proof travels with an identifiable revision and its evidence.
The lead integrates the reviewed steps into one candidate revision.

Integration is another boundary to test.
Passing step branches do not establish that their combination is correct.
Shared configuration, dependency resolution, and initialization order can change there.
Even a final configuration value or packaging edit belongs in the reviewed diff.

Before a long run, the worker sends a checkpoint with the actual inputs.
The checkpoint identifies the revision, environment, planned check, and evidence location.
It is a progress record, not a result.
The worker retains the exit status and delivery receipt when the work finishes.

### Keep the acceptance bar and the cold sweep separate

The acceptance bar repeats the agreed checks against the integrated revision.
It can cover clean builds, tests, packaged contents, and isolated smoke checks.
It answers whether the candidate satisfies the contracts already encoded in that bar.

The cold sweep starts from the same frozen revision with a different question:
which claims or failure paths has the team not adequately tested?
The reviewer reads the diff and runs independent probes without an expected verdict.
Use a different model vendor to add another source of independent judgment.

Keep the reviewer separate from the author of the affected change.
Provide scope and safety constraints without leading with the implementer's conclusions.
Withhold the other review's verdict until both independent records are complete.
Model diversity supports independence; it does not replace evidence.

Record the acceptance result and the cold-sweep verdict side by side.
A green bar can coexist with a rejected sweep because they test different claims.
For example, an existing suite may pass while a new stale-state probe exposes a fault.

When that happens, retain both results and reopen the affected slice.
Assign each finding a fix, an owner decision, or a justified residual disposition.
Reintegrate the fixes and repeat the affected probes at the new revision.
Recheck the acceptance bar required by the changed scope.

A release remains a separate authorized action.
The [assurance policy](docs/ASSURANCE.md) explains the project's release evidence
and review requirements in more detail.
Neither a scan nor a favorable review is permission to deploy.

### Record which way each error points

Severity and direction answer different questions.
Severity describes the impact; direction describes what the system gets wrong.
Record both when deciding whether a finding blocks the slice.

| Direction | Example consequence |
|---|---|
| False grant | A caller receives access it should not have |
| False completion | Work is marked finished before its effects succeed |
| False classification | Stale state causes the wrong transition |
| Refusal or unknown | The system visibly declines to make an unsupported claim |

A visible refusal still has an availability cost that needs an owner.
It is different from silently returning a confident but false result.
Renew accepted limitations by measurement at the current close.
Retire them with evidence rather than quietly removing their register entries.

### Route models deliberately

One concrete fleet pattern uses a mid-tier model for workers, a
stronger model for the reviewer, and a different vendor at its highest
reasoning effort for the closing cold sweep on each slice. This is a
selected operating profile, not an agenttalk-enforced default — see
the routing table in the [agent operating manual](docs/AGENT-MANUAL.md)
for concrete model/effort choices.

Keep the model profile stable enough to preserve useful working context.
Change effort when the task's risk or observed difficulty warrants it.
Do not turn routine coordination into an expensive review task by default.

The [agent operating manual](docs/AGENT-MANUAL.md)
explains effective model selection and session resets.
An effective model or effort change can reset a wrapped conversation.
Choose provider-supported profiles and record the settings used for important reviews.

### Keep execution inside the agreed boundary

Use project-local toolchains with recorded versions and build inputs.
Give every test service an owned endpoint and a fresh data directory.
Verify the actual process and listener before allowing a test to use them.

Under a local migration policy, production systems remain out of reach.
Deployment scripts and descriptors may be read or edited for review.
They are not executed, including purported dry runs or tool-based validation.
The owner receives a separate deployment checklist and its remaining prerequisites.

Disable unwanted startup telemetry before starting a component.
Use network controls and capture evidence to check the boundary when needed.
A disabled setting alone does not establish a history of zero connection attempts.
Record earlier unobserved traffic as unobserved rather than retroactively attesting it.

The local bus and static scanner do not make a coding model's session offline.
Model-provider access needs its own approved privacy and hosting arrangement.
Apply the migration's network policy to child processes and application libraries too.

Place temporary output in an owned task area.
Make child processes inherit that location and verify where they actually write.
Check files as well as directories when auditing temporary artifacts.
The [scratch hygiene guide](docs/ops/scratch-hygiene.md) explains cleanup ownership.

At close, preserve the evidence that must outlive the workspace.
Stop only the services whose ownership was established for the run.
Remove disposable work and report anything deliberately retained.
Cleanup is part of the proof, not an unrecorded chore after it.

### Feed the next slice with what this one taught you

End the work record with a tooling note, even when there was no friction.
Describe an observed cost or failure with evidence that another person can inspect.
Useful notes include delayed delivery, ambiguous status, quoting problems,
and cleanup checks that produced misleading results.

The lead turns recurring friction into an issue with an owner and a testable outcome.
A reviewed implementation then turns that lesson into a tool or stronger check.
Retest the triggering case before calling the improvement complete.
An issue or a retrospective note alone is not a shipped feature.

Carry accepted lessons into the next slice's briefing and guardrails.
Keep their evidence and scope so the next team can decide whether they still apply.
The migration then improves both the application and the method used to change it.

---

## 5. Technical reference and FAQ

This is a map of the CLI surface and the gotchas that don't fit
naturally into setup or use-case prose. It intentionally doesn't
duplicate the deeper docs — follow the links at the end for full
detail on any one area.

### Optional features at a glance

The two-agent handoff is the whole essence, and it stays simple:
`send`/`reply`, or the `/agenttalk.handoff` skill, for one agent to
hand work to another and block on the answer. Everything else is
opt-in, added to support real multi-agent work once a pair grows into
a team or runs unattended:

- **Named teams** — roles, groups, a lead/operator-liaison identity,
  and `broadcast` fan-out to a role or group.
- **Operator safety** — `rescind` so a stale request can't quietly get
  actioned (sending a newer request in its place does not do this by
  itself), a pre-action `check` that stops on a rescinded request and, with
  `--gates`, on a gate at HOLD, and epoch barriers.
- **24/7 supervision** — a background monitor that restarts agents
  across provider outages or stuck turns (the generated monitor is
  Windows-only today, see [#356](https://github.com/zoolok17/agenttalk/issues/356)), and a progress wrapper (`agenttalk wrap`) that
  tries to resume the agent's actual session context rather than starting
  the turn over, and starts a fresh session when that one cannot be used.
- **Shared ownership** — a `domain` registry mapping repo areas to
  owners, reviewers, and curators, with a scoped `lane` deliver-gate
  built on top of it.
- **Durable memory** — an `onboarding` ledger for what the team learned
  about a codebase before touching it, and a `knowledge` layer for
  pointer notes and lessons that outlive any one session. Accepted lessons
  that match a task are added to a wrapped seat's turn, up to five at a
  time, and shown by `agenttalk sync` to a seat run by hand; they are
  advisory reminders.
- **Assurance** — a `gate` HOLD/GO state plus typed review evidence, so
  a milestone can't close on the strength of an unreviewed claim.
- **A read-only dashboard** — a local web console (`agenttalk serve` /
  `agenttalk dashboard`) for watching roster, threads, and obligations
  without joining the bus yourself. On a large store its attention and
  lead-chat views still scan the whole store when their cache is cold,
  which can be slow ([#251](https://github.com/zoolok17/agenttalk/issues/251)).

### Command reference by category

Every command below is a real, currently-shipping subcommand
(`python -m agenttalk <cmd> --help`); flags shown are the ones most
relevant day to day, not exhaustive lists — run `--help` on any
command for the full set.

**Messaging**

| Command | What it does |
| --- | --- |
| `send` | Point-to-point message. `--kind`, `--to`, `--await-reply`. `--kind task`/`rescind`/`end` are refused here — use their dedicated commands. |
| `task` | A lead work order (#163). Gated at write time: sender must be the roster's sole `role=lead` or its `operator_facing` liaison (live config, not the caller's claim); the RECIPIENT is checked against the fixed version that first understood `task` (0.88.0) — other seats do not matter — and the send is refused if it predates that floor unless `--force`. `broadcast --kind task` checks only the seats that get a copy. Recipient replies `reply --kind task-response --meta status=accepted\|declined\|done`. |
| `reply` | Answer the latest (or a specific, via `--to-id`/`--to-request`) received message. `--na` for a non-substantive close (rejected on `review-request`/`proposal`/`task` threads, which need a typed response). |
| `broadcast` | Fan-out to `--to-group`/`--to-role`/`--all`; `--resume` recovers a partial fan-out. |
| `wait` | Block for the next message. `--to-request` scopes to one thread; `--timeout 0` waits forever. |
| `recv` / `drain` | Read without blocking; `drain` consumes everything currently queued. |
| `threads` | List open request/reply obligations. |
| `sync` | Roster + open threads + next-action digest for one agent. |
| `ack` | Advance a cursor (global or `--to-request` scoped) without replying. |
| `whoami` | Resolve identity from env/config. |
| `tail` | Follow recent messages. |
| `compact` | Archive a safe prefix of old messages into cold storage. |
| `transcript` | Export the session so far as markdown. |
| `end` | Close a session and write the transcript. |

**Identity and roster**

| Command | What it does |
| --- | --- |
| `roster add` | Add an agent (idempotent). `--role`, `--group`, `--unique` (refuse if the name is already live). |
| `roster remove` / `retire` | `remove` is refused by default; `retire` tombstones permanently and keeps history valid. |
| `roster rename` | Retire the old name, add the new one, carry over role/groups. |
| `roster set-role` / `set-group` | Assign a free-form role label; define a group's membership. |
| `roster set-operator-facing` | Designate the single agent the human talks to directly. `--clear` to unset. |
| `roster set-trust-class` | Opt-in non-authority model trust metadata. |
| `avatar` | Per-agent display avatar for the dashboard. |
| `domain` | `{validate,list,show,check-path}` — read-only inspection of the ownership registry. |

**Review and assurance**

| Command | What it does |
| --- | --- |
| `gate {set,list,check,waive}` | Lightweight `HOLD`/`GO` assurance state; `check` exits 3 on an unwaived blocker. |
| `close {open,ack,draft,counter,check,publish,reopen,acceptance attach,acceptance cold,acceptance successor,list,show}` | Aggregates gates + typed review evidence into one milestone/release verdict. Acceptance open adds `--acceptance-plan PLAN --project-repo PROJECT`; attach takes `--file BUNDLE --from ACTOR`; cold phases commit observations before attachment and reconcile after reveal. Schema-3 cooperative GO requires bound reproducer/independent reviewer accepts and execution/offline/close-out hygiene evidence. `show` lists successor alternatives. Per-attempt operator amendments preserve original failures; recovery roots retain related-change obligations. See the [acceptance guide](docs/ACCEPTANCE.md) and [implementation contract](docs/STEP-ACCEPTANCE-INC1.md). |
| `close signoffs {plan,apply,override}` | Derives specialist sign-off routing by risk class. |
| `check` | Pre-action HOLD/GO check for one request: `agenttalk check --for <seat> --to-request <request-id> --gates`. It says stop (exit 3) for a rescinded request; `--gates` adds a stop for any gate at HOLD (without it the gates are not checked, and the bundled lead instructions leave it out today, #362), and `--epoch` adds one for a request older than the current epoch barrier. A newer request sent in place of an old one does not stop the old one: rescind it. |
| `lane {assign,check,deliver,status,approve-shared}` | Scoped deliver-gate: bounds a change against the domain registry and other active lanes. |
| `onboarding {create,list,show,state,record}` | Durable first-pass ledger: segments, claims, drift, unknowns. |
| `comprehension {scan,status,report,validate,prune}` | Offline static comprehension inventory (features, units) for one legacy repository; `pack` and the HTTP surface are planned, not yet built. |
| `knowledge {publish,curate,pull,search,onboard}` | Durable pointer notes and lessons hung off the domain registry. |
| `dev-gate` | Build + test evidence a `review-request`/`review-result` can point to: source and wheel, per Python minor, aggregated across CI legs. |

A `review-request` (`send --kind review-request`) is an ordinary
message with `base_sha`/`head_sha` meta and a body naming what changed,
how to verify, and where to focus. A `review-result` with `--meta
status=approved` must carry typed evidence: `risk_class`,
`release_blocker`, `tests_referenced`, `tests_executed`,
`evidence`/`artifacts`, and `residual_risk` (or `na_reason` for any
field that's `n/a`). `close ack --status accept` reuses that same
typed-evidence shape at the milestone level.

**Operator-facing and safety**

| Command | What it does |
| --- | --- |
| `escalate` | Route a question to the operator-facing liaison. |
| `attention` | Operator attention queue. |
| `rescind` | Supersede a request so a stale answer can't quietly close it. |
| `barrier` | Epoch barrier operations. |
| `relay` | Relay between agents/liaison. |
| `prune` | Housekeeping over stale state. |
| `release` | Stand an agent (or group/all) down, with a required `-m` reason; `--relay-human` vs `--emergency`. |

**Lifecycle and unattended operation**

| Command | What it does |
| --- | --- |
| `wrap` | Structured-stream adapter around a CLI child: visibility, working-turn heartbeat, degraded-output detection. `--loop` for supervised long-running mode, `--one-shot` for a single turn, `--turn-events` for the optional turn journal. |
| `supervisor` | Read-only status over the supervisor's own state file. |
| `supervise` | Scaffold (`--init`), preflight (`--bootstrap-check`), and script-refresh (`--refresh-scripts`) operations for the external monitor. |
| `dead-letter {list,show,requeue,resolve,purge}` | Messages that exhausted automatic retry. |
| `managed-lead-loop {set,clear,list}` | Configure which identities may run the leased lead-loop controller. |
| `deadman` | Threshold check for unresponsive agents. |
| `checkpoint {save,resume,show}` | Capture/restore context headroom, git state, and actionable threads across a compaction. |
| `heartbeat` | Stamp liveness; `--hook` mode never blocks a tool call. |
| `request-restart` | Bounce one agent on demand; resumes its session. |
| `request-launch` | Request a fresh ephemeral agent for a profile/skill/revision. |
| `commit-gate {status,reset}` | Per-agent breaker state and authenticated reset. |

**Workspace hygiene and setup**

| Command | What it does |
| --- | --- |
| `init` | `--here`/`--path`, `--agents`, `--force` (config only, not messages). |
| `scratch root` | Resolve/create `<scratch_root>/<agent>[/<task>]`. Wrapped seats are told to keep their temporary work here, but agenttalk does not yet point the temp files of the programs they run here; by default those still go to the user's temp folder ([#336](https://github.com/zoolok17/agenttalk/issues/336)). |
| `janitor` | Report (default) or `--apply` cleanup: WIP-commits dirty **registered worktrees** on their own branch (never the default branch, never a detached HEAD), removes allow-listed scratch paths, and prunes stale worktree registrations; `--keep-days`. Before each delete it checks, without following links, that the scanned root and every folder below it down to the candidate's parent are still plain folders (a link there keeps the candidate), and that the candidate's link-or-not status is what the scan recorded: one that became a link is kept and reported, and one that was already a link is removed as the link itself, with what it points to left alone. A folder link found inside a removed folder is removed as a link, not entered (a swap made by another process during the delete itself is out of scope). A path the ordinary delete cannot remove is listed under `FAILED` with the reason, never forced; the remaining files are left as they are after the failed attempt (files removed before the failure stay removed, and read-only flags may have been cleared), with no ownership or permission takeover ([#342](https://github.com/zoolok17/agenttalk/issues/342)). |
| `doctor` | Health check; `--json` for automation. |
| `reset` | Clear active bus state; `--archive` preserves it instead of deleting. |
| `capacity {show,refresh}` | Publish or read usage readings: each account's 5-hour and weekly windows and each seat's conversation fill. Old readings show as stale. See [Reading the capacity files](#reading-the-capacity-files). |
| `codex-config` | `--enable`/`--disable`/`--status` for Codex sandbox approval settings. |
| `install-skills` | Install bus skills (and the dev-discipline devkit) for Claude and/or Codex. |
| `hmac-init` | Provision HMAC signing material. |
| `backup` | (#156) Write a verified out-of-tree snapshot of the store, keyed by project identity; `--json`. See [docs/BACKUP.md](docs/BACKUP.md). |
| `gateway` | `{init,task-install,start,stop,status,...}` — lifecycle of the optional managed model gateway, the supported route for gateway-backed seats: OVH AI Endpoints with the Qwen3.8-27B model, pinned in code. Start it through the managed task; `gateway run` is the internal service entry that task launches, not a supported way to start it by hand. |

**Dashboards**

| Command | What it does |
| --- | --- |
| `serve` | Single-project read-only web view. Loopback-only (`127.0.0.1`/`::1`/`localhost`); no flag exposes it beyond that. |
| `dashboard` | Same server, multi-root obligation view under `/dashboard`. `--store` is repeatable. |

### Reading the capacity files

**In plain words.** Each seat writes its latest usage reading to a small file: how full its
account's 5-hour and weekly usage windows are, and, when agenttalk can tell, how full the seat's
own conversation is. The usage windows belong to an account, not to a seat: all Claude seats that
read the same Claude folder share one Claude account, and the same holds for Codex. Each figure counts for 10
minutes from when it was seen; after that agenttalk hides it everywhere it shows it, even when
nobody rewrote the file, so an old number never looks current. The file is advice only: nothing
in agenttalk waits for it or is held back by it.

**What you will see**

- `agenttalk capacity` lists the seats that share an account under one line naming that account.
  Below it, each seat shows its own reading, newest first, with how long ago it was seen. Readings
  are never merged across seats: to judge the account, read the newest seat's row.
- `agenttalk status` adds `capacity=current`, `capacity=stale(last reading ...)` or
  `capacity=unknown(<reason>)` to each seat that has written a reading.
- The web console and `agenttalk attention` show and warn only on figures that are still current.

**Where the figures come from**

- **Claude seats:** first the seat's own rate-limit messages, the `rate_limit_event` lines the
  Claude command line sends during a turn. They say whether a request was allowed. Some also say
  how full each window is; an ordinary turn's message may say only "allowed", and then the
  reading records exactly that, with no percentage and no window. When there are no current
  messages, agenttalk uses the status-line dump, `statusline-last-input.json`, which exists only
  when a Claude status line is set up to write it; when both are old, it names the newer one.
  - Both come from one Claude config folder: the one the seat's Claude actually uses
    (`CLAUDE_CONFIG_DIR`, a gateway seat's own profile folder, or `~/.claude`). The account is named
    after that same folder.
  - A saved reading remembers the account it was taken under. When the seat later runs under
    another folder or provider, the old reading is dropped, never relabelled.
  - A manual `agenttalk capacity refresh --for <seat>` run by the seat itself (`AGENTTALK_SELF`)
    finds its folder and provider the way its wrapper does. Run for another seat, it never reads a
    folder of the shell that runs it: that shell's `~/.claude` and `CLAUDE_CONFIG_DIR` say nothing
    about the other seat's. It publishes the seat's own saved reading again, under the account its
    wrapper bound, or reads the `--statusline-path` you give.
  - A folder is read only when it matches the account of the seat's saved reading. Otherwise the
    reason is `claude_seat_source_unknown`.
  - For a seat whose supervisor entry gives it its own `CLAUDE_CONFIG_DIR`, pass that folder's
    `statusline-last-input.json` with `--statusline-path`, or let the seat's wrapper refresh it.
  - With neither source, the reason is `claude_source_not_configured`.
- **Codex seats:** the seat's own session file under its `CODEX_HOME`. A seat without a Codex home
  of its own reads the shared `~/.codex` (also when `CODEX_HOME` names that same folder, directly or
  through a link). There it
  reads only the file of its own session: the file named with its thread id, or one whose session
  record declares that id.
  - A mention of the id inside another session does not count.
  - A manual `agenttalk capacity refresh` uses only the thread id the seat's wrapper saved, never
    the thread of the program that runs the command.
  - Without a thread id the reason is `codex_no_thread_yet`.
  - Separate Codex homes seeded from one login show as separate accounts, although they share one
    budget.

**Fields other programs may rely on** (in `.agenttalk/state/<seat>.capacity.json`)

| Field | What it means |
| --- | --- |
| `schema_version` | `2`. A file without this field was written by an older agenttalk. |
| `source_agent` | The seat that wrote the file. |
| `source` | Where the figures came from: `claude_stream` (the seat's own messages), `claude_statusline`, `codex_rollout`, or `unknown`. |
| `observed_at` | When the newest part of the reading was seen (UTC, ISO 8601), not when the file was written. |
| `confidence` | `observed` while any part was current when the file was written; `stale` when none was (no figures); `unknown` when there is no reading (see `reason`). |
| `reason` | Why there is no current reading, for example `claude_source_not_configured`, `claude_seat_source_unknown`, `codex_no_thread_yet`, `codex_no_reading` or `claude_statusline_stale`. `no_figures_in_event` means the seat's messages gave a status but no percentage. |
| `scope`, `account` | `scope` is `account`: the window figures describe the account named in `account`, written `<provider>:<OS user>:home-<hash>`, where the hash names the folder the reading comes from, the default `~/.claude` or `~/.codex` too. Seats with the same `account` share one budget. |
| `primary_*`, `secondary_*` | The 5-hour and the weekly window. `_used_percent` (0 to 100, above 100 past the limit), `_resets_at` (Unix seconds; a value outside any real date is dropped), `_status` (`allowed`, `allowed_warning` or `rejected`, when the provider says), `_window_minutes`, `_window_basis` (`measured` when the provider gave the length, `assumed` when agenttalk filled in 300 or 10080), and `_observed_at`, when that window's figures were seen (`null` means `observed_at`). Each may be `null`. |
| `last_status`, `last_status_at`, `last_status_window` | The provider's verdict on the seat's latest request (`allowed`, `allowed_warning` or `rejected`), when it was seen, and the window it named (for example `seven_day`), or `null` when it named none. A refusal is a refusal even when that window's percentage reads low. |
| `rate_limit_reached_type` | The window that refused a request, when one did. It counts only while that window's own figures are current. |
| `context_used_percent`, `context_window_size`, `context_tokens` | The seat's own conversation fill, seen at `observed_at`, filled in only when agenttalk can tell it is this seat's: from the seat's own Codex session file, or from a Claude status-line dump that names the seat's own session. Otherwise `null`. The status-line dump is shared by every Claude session of the OS user. |

**Judge each part's own age when you read the file.** A window's figures count only while its
`_observed_at` (or, when that is `null`, `observed_at`) is at most 10 minutes old. The same holds
for `last_status` and `last_status_window` with `last_status_at`, for the conversation fill with
`observed_at`, and for `rate_limit_reached_type` with the window it names.
agenttalk's own readers do this; a file nobody rewrites goes stale without saying so.

Other fields, such as `plan_type` and `limit_id`, are not part of this list and may change.
agenttalk replaces the file as a whole, but on Windows that can fall back to rewriting it in
place, so a reader can catch it half written: treat a file that does not parse as "no reading"
and read it again later.

### When a Claude seat runs out of its allowance (the usage-limit park)

**In plain words.** A Claude seat that has used up its 5-hour or weekly allowance cannot do
any work until the allowance comes back. Before, its wrapper kept starting the model again every
few seconds for four hours (hundreds of tries) and then threw the message away. Now the wrapper
**parks** the message: it makes one try, sees the proof that the account is out of allowance,
and stops. The message stays at the front of the seat's queue, nothing is lost, and the seat
stays alive and says so. This is on by default.

**What you will notice**

- `agenttalk status` flags the seat `usage_limit_parked(until=<time>)`. The time is in UTC, and
  it is the reset time Claude stated. The flag reads `until=restarted` when no time is known
  and `wrapper_not_responding` when the wrapper stopped refreshing its status.
- `agenttalk attention`, `agenttalk doctor` and both web consoles show it too, as **PARKED**
  ("parked on a usage limit until ..."). A parked seat needs a look, but it is never shown as
  down, never as "config blocked", and never as "not for you".
- When several facts disagree, the order is: the supervisor's verdict that the seat is stuck or dead,
  then current working evidence, then a fresh park, then old working history, then an old park.
  `agenttalk status`, `agenttalk supervisor` and both web consoles (the seat's row and its
  attention card) apply the supervisor's verdict: a seat it calls stuck or dead is shown that way,
  never as "parked" and never as "idle". On the two command-line screens the line reads
  `health=STUCK_OR_DEAD (wrapper self-reports idle_waiting)`: the verdict first, the seat's own
  report only as a labelled aside. The `--json` output keeps the seat's own report as plain data
  beside the verdict. `agenttalk attention` and `agenttalk doctor` do not consult the supervisor,
  so their text says "(supervisor not consulted)".
- The liaison gets one notice per park, in plain words, with the two ways to act.

**What the seat does by itself**

- It tries the message **once** each time its wrapper is started again.
- When Claude states when the allowance comes back, it tries once **30 seconds after** that
  time. If it is still limited and Claude states a later time, it waits for that one. The
  stated time must be in the future and at most 8 days ahead; otherwise there is no timed try.
- It never retries on a timer otherwise, and it never reads a reset time from message text.

**What you can do**

| You want to | Run |
| --- | --- |
| Start the seat again now (one try) | `agenttalk request-restart --for <agent>`. It needs a running supervisor; without one, stop the wrapper and start it again. A protected seat (the operator-facing liaison or a lead) also needs `--force-protected` and, because a parked seat is alive, `--acknowledge-live-protected-kill`. `--clear-restart-budget` alone does not relaunch. |
| Skip the parked message | `agenttalk ack --for <agent> --id <message id>`. It moves the seat past the message **without processing it and without a dead-letter record**, so it cannot be requeued from the dead-letter sink. It is refused for a managed lead-loop agent. |
| Get the old behaviour back | Set `AGENTTALK_STOP_RETRIES_AT_LIMIT` to `0`, `false`, `off` or `no` (any capitals, spaces around it are ignored) in the wrapper's environment. It is read once when the wrapper starts. Any other value, or leaving it unset, keeps the park on. The one switch covers all three kinds of park: a usage limit, and the overloaded and throttled cool-downs described below. A message that already carries a park when you switch it off is driven as before, but the attempts and the time it spent parked stay out of its disposal counts. |

`agenttalk doctor` warns about a seat parked for more than 24 hours (set
`AGENTTALK_USAGE_PARK_WARN_AFTER_HOURS` to change it) and lists a park notice that never reached
anyone.

**What counts as a usage limit.** Only proof from the seat's own output counts: Claude reported a
rejected usage event for a known window (`five_hour` or `seven_day`), and its final result is an
error. A final result that is not an error (`is_error` false), a missing final result, a watchdog
kill, a bus fault or an unknown window keeps today's behaviour. A non-zero exit **alone** is not
proof and local causes keep their own handling, but a non-zero exit after a proven provider error
still parks. Message
text never decides anything: a weekly-limit message that Claude words as "prompt too long" is still
a usage limit and is never counted as a bad message.

**With the turn journal on.** The journal records the turn that proved the limit as one failed
dispatch, with the same failure class it has when this switch is off. While the seat is parked the
journal hears nothing more about the message (a park is not a consumption, and no second dispatch
starts). When a later try succeeds, the journal records a new dispatch and then exactly one
`completed` for that message. In the new web console a parked seat is held to the rule of a stalled
warning: it always counts as needing attention, is never "All quiet" or "not for you", and no saved
"Later" choice can hide it.

**Limits to know**

- A parked message blocks the messages behind it, including `release` and `end`, exactly as the
  existing retries and the config-blocked park do. Stop the wrapper or skip the message.
- Only the standard wrapper path parks. A seat under a commit-gate policy that owes an answer, and
  the one-shot reviewer launches, behave as before.
- Claude only. A Codex usage limit or overload keeps today's behaviour for now. For a Claude seat,
  an overload (HTTP 529) and a suspected limit now cool down instead (see the next section).
- The notice is not exactly-once: a crash between sending it and recording it can repeat one
  notice (the same thread). An unrouted notice is tried at most four times, 15 minutes apart;
  `agenttalk doctor` lists it.
- A seat that is parked is alive, so the supervisor does not restart it and spends no restart
  budget; a parked wrapper that stops answering is recovered like any dead one.
- A change of the clock can cause one early try, which cannot repeat: a try that finds the same or
  an earlier reset schedules nothing.
- The `_epoch` fields a parked seat publishes are rounded down to whole seconds, so a status can be up to one
  second late in turning from "parked" to "wrapper not responding"; it is never early.
- The test record that proves the off switch (`tests/golden/`) compares normalised observations,
  not bytes: it does not cover formatting, duplicate keys, a final newline in a file of JSON lines,
  or how files are locked. It hides values by name (the release number, a stored `size_bytes`, an
  attempt id), at any depth, so a future field with one of those names needs a look. Only JSON
  objects and arrays are decoded; a file holding a bare JSON string is compared as plain text.

#### Reading the park marker from another program

**In plain words.** While a seat is parked, its wrapper keeps one small file up to date that says
so. Other programs may read that file, for example to show or react to a parked seat. It holds
only words and numbers, never message text, and it carries a version so a reader can tell when the
format has changed. It is a **view** of the park, not the park itself: a program that looks at it
only now and then can miss a short park entirely, and a file can be left behind (see "How long it
lasts").

**Where it is.** `state/usage-limit-park/<agent>.json` under the bus root, one file per seat. A seat
that is not parked has no file.

**Fields.** One JSON object. No other keys are written.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | The version of this format: `1`. |
| `agent` | text | The seat's name; the same as the file name. |
| `state` | text | Always `usage_limit_parked`. |
| `provider` | text | Whose allowance ran out. Today only `claude`. It comes from the proof the wrapper holds, never from the seat's name. |
| `window` | text or null | Which allowance the **latest** refusal named: `five_hour` or `seven_day`. |
| `reset_epoch` | integer or null | When the provider says the allowance comes back: whole seconds since 1970, UTC. See "Reset and window" below. `null` when no reset time is known. |
| `wake_epoch` | integer or null | When the wrapper will try again by itself, in the same unit (the reset plus 30 seconds). `null` when it will not: no reset known, or that try was already used. |
| `message_id` | text | The message the seat is holding. |
| `parked_at` | text or null | When the **current** park began: the moment the wrapper saw the provider's refusal, as an ISO 8601 UTC time that may carry fractional seconds. It does not change when the file is refreshed or when a later try is refused for a usage limit again. If a try fails for another reason, that park ends; a later limit refusal of the same message starts a new park with a new time. |
| `wrapper_generation` | text or null | Identifies the wrapper run that wrote the file. |
| `updated_at_epoch` | integer | When the wrapper last refreshed the file: whole seconds since 1970, UTC. It shows that the wrapper is alive. It is **not** a new observation from the provider. |

**Reset and window.** The first refusal sets both. After a later refusal of the same message (for
example a try that was refused again): the window is the latest refusal's; the reset is replaced
only by a **strictly later** reset. A later refusal that states no reset, or the same or an earlier
one, keeps the reset already held. So `reset_epoch` is the latest reset seen, and the file can say
`seven_day` beside a reset that came from an earlier `five_hour` refusal.

**The version rule.** A reader must refuse a file whose `schema_version` it does not know (and a
`provider` it does not know), and must not guess what it means. A new version means the format
changed.

**Is it current?** Judge it from `updated_at_epoch`, never from the file's modified time. The
wrapper refreshes the file about once a minute. If it has not been refreshed for 300 seconds
(`MARKER_STALE_SECONDS`), the wrapper is not responding: the seat may still be parked, but nothing
confirms it. The `_epoch` fields are rounded **down** to whole seconds, so an age is right only to
within one second; `parked_at` keeps the precision it was recorded with. **A fresh file is a sign of a
live parked seat, not proof:** a wrapper stopped abruptly leaves a file that still looks fresh for up
to 300 seconds. A stale file may be a leftover.

**Reading it safely.** Several things can go wrong while the file is written or read. Each one means
"read again a little later": never "no parked seat", and never "parked".
- Normally the wrapper replaces the file in one step, so a reader sees the old version or the new
  one, never half of each.
- The file can disappear between listing the folder and opening it.
- On Windows a read can fail for a moment while the file is being replaced.
- On Windows, when replacing keeps failing (some sandboxes forbid it), agenttalk falls back to
  writing the file **in place**. A reader can then see a partly written file. Treat a file that
  does not parse, or lacks a field, as "read again later".

**How long it lasts.** The wrapper removes the file when the park ends or changes: when the held
message is delivered, skipped or otherwise gone; when the wrapper starts (it writes the file again
if the seat is still parked); and when a try after the reset begins (it writes it again if that try
is refused). If the wrapper is stopped abruptly, the file stays behind. agenttalk's own readers also
ignore a file whose message the seat has already moved past, or whose wrapper has since been
replaced; another program cannot check either, which is why a stale file must not be trusted and a
fresh one is only a sign.

**What it never holds.** No message text and no text from the provider: only the words and numbers
above. A cool-down (the overloaded and throttled kinds, next section) writes **no** marker at all.

**Technical detail.** The park is recorded in the message's attempt record (see
[docs/DESIGN.md](docs/DESIGN.md) section 4.9) and published for readers as
`state/usage-limit-park/<agent>.json` (closed words, numbers and times only). Its health is the
existing `rate_limited_or_outage` state with the reason `usage_limit_parked`. It replaces an earlier
proposal (pull request #104, never merged) that read the provider's error text; this one uses
structured signals only.

### When the AI provider is overloaded or throttling (the cool-down)

**In plain words.** Sometimes the provider answers "too busy" (an overload, HTTP 529), or answers
with an error that looks like a usage limit but cannot be proven (a plain 429 without limit
details, a status or window word the wrapper does not know, or a rejection that no error result
confirms). Before, a Claude seat started the model again every fraction of a second to two seconds
for hours, and then threw the message away. Now the wrapper **cools the message down**: it keeps
the message at the front of the seat's queue, nothing is lost, the seat stays alive and says so,
and it tries again after 15 minutes, then after 30, then every 60. This is on by default, and one
switch controls it together with the usage-limit park above.

- **Overloaded.** The first two failures are retried after the loop's usual short back-off, the way any
  ordinary failure is (these are counted normally). A third failure in a row starts the cool-down.
- **Throttled.** The first failure starts the cool-down; there is no quick retry.

**What you will notice**

- `agenttalk status` flags the seat `provider_wait_parked(kind=overloaded,retry=<time>)` or
  `provider_wait_parked(kind=throttled,retry=<time>)`. The time is in UTC and is the seat's own saved
  next try. The flag reads `retry=soon` while a try is running and `wrapper_not_responding` when the
  wrapper stopped refreshing its status. `status --json` carries the same facts as `kind`,
  `next_try_epoch` and `park_rev` inside `usage_limit_park` (older records have none of the three and
  read as a usage limit). The supervisor row and the `doctor` data carry `kind` and `next_try_epoch` only.
- `agenttalk attention`, `agenttalk doctor`, `agenttalk supervisor` and both web consoles show it,
  as **PARKED**, in its own words: "waiting for an overloaded AI provider; tries again at ..." or
  "waiting on a possible usage limit; tries again at ...". An overload is never called a usage limit.
- The seat's health is the existing `rate_limited_or_outage` state with the reason
  `provider_wait_parked`. The cause is in `reason_detail` (`status_529` or `status_429`, plus the
  subtype when it is one the wrapper knows) when there is a fitting word, and absent otherwise.
- The liaison gets one notice when a message starts cooling down, and one more each time the kind
  changes, in plain words. Not one per try. Its subject is "provider-wait park notice" (a proven usage
  limit keeps "usage-limit park notice"), and it names only the saved next try, never a schedule after it.

**What the seat does by itself**

- It tries again at the saved time: 15 minutes after the first failure, then 30, then every 60. Each
  saved time is used once.
- **A restart does not make it try sooner.** The saved time wins, so a restarting wrapper cannot hammer
  a busy provider. A saved time that is missing, malformed or already used, or that lies further ahead
  than any wait the schedule sets (for example after a clock change), is replaced once and not tried at
  once. A time that is plausible but was altered is **not** detected.
- The wait and the tries made under it **never count** towards "100 attempts / 4 hours". The message
  is never thrown away while it cools down.
- A usage limit whose retry at the reset was never used keeps that retry through a cool-down: it is set
  aside (`quota_wake_epoch`) when the cool-down starts, and comes back when the same limit returns, if
  it is still ahead. It is trusted only when it is exactly the reset's own retry (the latest proven
  reset plus the 30-second margin); any other saved time is dropped and nothing is armed. A later reset
  replaces it; a retry that was already used is never set aside or re-armed.
- If a try meets a proven usage limit, the park becomes a usage-limit park (with its marker and its
  rules); if it meets something else that is not a provider wait, the cool-down ends and the message is
  handled as any failure.

**What you can do.** Wait, or skip the message with `agenttalk ack --for <agent> --id <message id>` (no
dead-letter record). Starting the seat again does not try sooner. To get the old behaviour for all three
kinds, set `AGENTTALK_STOP_RETRIES_AT_LIMIT` to `0`, `false`, `off` or `no`; a message that already carries
a cool-down is then driven as before, and its attempts and time stay out of its disposal counts.

**What counts.** Only structured proof from the seat's own output counts, and a later successful result
cancels it (the order of the stream decides): a `429` error result, a `529` error result, or a
`rate_limit_event` whose status is not `allowed` or `allowed_warning`. Message text never decides
anything, and `allowed_warning` is harmless on its own. A usage limit that the stream proves (the park above)
is decided first and is unchanged.

**Limits to know**

- Claude only. A Codex seat keeps today's behaviour.
- **A cool-down is not published in the marker.** The version-1 marker file above is written for a
  proven usage limit only; a cool-down is carried by `status --json` and the other readers.
- **A hand-edited or damaged attempt record is only partly detected.** The wrapper checks that a value
  is well formed and, for the saved quota retry, that it matches its reset. It cannot tell a plausible
  but altered value from a true one, because no second history exists to compare it with. A changed
  `cooldown_step` shortens or lengthens a wait within the schedule's 15, 30 or 60 minutes; a plausible
  future `wake_epoch` can delay a try within the accepted 62-minute window (the longest wait plus a two-minute margin); a changed "already used" marker can
  cause one extra try or hide an unused retry.
- A message that keeps meeting a suspected limit is never thrown away: it is tried every hour for as long
  as it takes, and the messages behind it wait. It is visible in every screen, and `agenttalk doctor`
  warns after 24 hours (counted from the start of the whole wait, also across a change of kind).
- The quick retries of an overload are real attempts: a message that already has 19 eligible attempts
  reaches the 20-attempt escalation with the first, and one with 98 eligible attempts and four eligible
  hours reaches the disposal with the second. The promise of no disposal starts when the cool-down starts.
- A terminal result that names only the provider's subtype (no number) is labelled in the health file but
  does not start a cool-down.
- The 529 and plain-429 cases in the tests are **made up**: no real sample of either exists.

**Technical detail.** New fields in the message's attempt record (added only, none renamed): `park_kind`
(`overloaded` or `throttled`; absent means a usage limit), `cooldown_step`, `soft_run`, `park_rev`,
`park_detail`. The next try is the record's `wake_epoch`, written only by `usage_park.arm_cooldown_wake`.
`park_rev` goes up on a new park and on every change of kind, and is what the attention card is
bound to (never the next-try time); it is absent on a history that only ever held a usage limit.

### Messaging-system internals

- **No daemon.** The bus is files under `.agenttalk/`; nothing has to
  stay running for messages to persist.
- **One JSON file per message**, prepared and atomically published
  under a store lock, so writers never race on a single message.
- **Global cursor plus per-thread state.** Reading the plain inbox
  advances a global cursor; a scoped `wait --to-request` advances only
  that thread's own `seen_msg_id`, so working one request doesn't
  consume unrelated traffic.
- **Polling, not watchers.** `wait` polls at `--interval` (default
  0.3s) and backs off adaptively up to `--max-poll-interval` while the
  bus is idle, resetting the instant new activity lands.
- **Both terminals show both halves.** `send` prints to the sender's
  stdout; the receiver's `wait` prints the same message on the other
  side. `.agenttalk/messages/` is the durable source of truth either
  way.
- **No transport.** Both agents are expected to share one project
  directory on one machine (or a directory synced by a mechanism you
  already trust) — there's no server process bridging machines.

### The turn journal (optional)

**In plain words.** A wrapped agent can keep a small journal of what its turns
did: when a turn was dispatched, whether a model process was really launched,
how it ended and how many tokens it used, and how each message was finally
dealt with. It is for anything that wants to count turns, runs or token usage
afterwards. It is **off unless you turn it on**, it only watches, and if it
cannot write, the wrapper carries on exactly as before: a turn never waits for
the journal, and the journal adds no file reading and no logging to a turn.

**Turn it on** for a supervised wrapper with `agenttalk wrap --loop --turn-events`,
or by setting `AGENTTALK_TURN_EVENTS=1`. It applies to `--loop` and `--lead-loop`
wrappers; there is no configuration-file setting. The files are kept in a
per-user folder beside the wrapper logs (see "Where agenttalk keeps files"),
one subfolder per agent; `AGENTTALK_TURN_EVENTS_DIR` moves the whole folder.

**What is recorded.** One JSON line per record, in files that only grow:

- `dispatch_started`: a turn was dispatched (the message, the turn, which
  CLI, a fresh or resumed session, and the message's own send time);
- `dispatch_ended`: how that dispatch ended: whether a process was launched,
  `success`, `failed` or `not_launched`, how it exited (`normal`,
  `spawn_error`, `exception`), a closed failure class, the duration, and token
  `usage` (input, output, cache read, cache write). A count that is not known is
  `null`, never 0; Codex turns carry no usage in this version;
- `message_disposed`: the message was durably consumed as work, with its
  `disposition` and five raw facts.

A dispatch is one call of the turn runner. A **launch** is a model process that
really started (counted from the moment it exists, even if setting it up fails
afterwards). A turn held back by a gateway hold or refused by a pre-launch check
is a dispatch that did not launch.

**The disposition follows the wrapper's own definition of a completed turn.**
`completed` appears only where the wrapper itself counts a completed turn, and
on the commit-gate path that means the message was consumed **and** its reply
landed. The words are:

- `completed`: as above;
- `dead_lettered`: the message was moved to the dead-letter area;
- `delivery_failed`: the commit gate recorded a terminal failure;
- `outcome_unknown`: everything else, for example compliance success without
  landed evidence, a message that ended as not owed, or a message consumed
  without any dispatch. The journal never reads the wrapper's ledger to look
  for more proof, so a message whose finished work the loop was only
  completing after an interruption is recorded as `outcome_unknown` as well,
  with the raw facts that are known (the rest `null`). That is the honest
  limit of this version.

The raw facts are recorded as well, so a reader can apply its own policy and a
later change in how agenttalk words a disposition never makes an old line
wrong: `consumed`, `landed`, `compliance_success`, `dead_lettered` and
`terminal_failure`. Each is `true`, `false`, or `null` where that path has no
such fact. The loop's own stand-down messages are control records, not work, and are not recorded.

**It says when it may be incomplete.** Every event carries a number (`seq`) given
when it is queued, so an event lost to a full queue, a failing disk or a clock
that gives no usable time leaves a visible gap; `dropped_total` counts what was
lost before it. Read events in number order, not by their time: an event's time
(`at`) is read when the event is reported, just before it gets its number, so
it can be earlier than the time of the event before it (two parts of a program
reporting at once, or the machine's clock set back). Each file starts
with a `stream_started` record (no number); a clean stop writes `stream_closed`
with the last number used, so a lost end shows. `streams.jsonl` lists every
stream ever started, so a stream whose files were deleted can still be seen.
Old files are removed oldest first once the folder passes its size cap.

**Limits to know.**

- "Complete" means "no loss the journal can see". It is not a guarantee.
- A run whose journal failed to start (or timed out starting, after at most
  2 seconds) is **not observed**: the wrapper simply runs without it, and
  `status` shows `off (start_failed)` or `off (start_timeout)`. Finding the
  journal's folder counts against those 2 seconds: it happens on the writer's
  own thread, so a slow or unavailable disk cannot hold up the wrapper's start.
- A message's time is the **sender's** clock, recorded only when it is a real
  time (a value that is not, such as a path or a name, is recorded as `null`).
  A consumer comparing it with the
  journal's own times should allow for skew between machines, and an undetected
  change of a machine's clock can defeat any time-based check.
- A crash can lose the last events that were still waiting to be written.
- When a full file is closed, the journal forces it to disk. If that fails, the
  fault is counted and recorded in the journal's status record, and nothing tries again: that
  file's durability is unproven. Its readable events are not counted as lost,
  because "complete" only ever means "no loss the journal can see".
- Once the journal is cancelled (a start timeout or failure, or close's
  deadline), the writer begins no new step. A step it has already begun (one
  helper: an append, an atomic status write, a sync, a registration read with its
  read-only process-identity lookup) may finish on the writer's own thread. No
  caller ever waits for it. After close times out, no new startup status write
  begins; a status write already started may finish on the writer's own thread.
- `status` and `doctor` add journal labels only once the project has a journal
  folder. A first-ever start failure that creates no folder is therefore not shown
  there, even though the wrapper's health record carries the warning.
- On macOS, where agenttalk cannot read a process's identity (its start token),
  `status` and `doctor` say the journal's state is unknown:
  `unknown (no process identity on this platform)`. There they cannot tell you
  that the writer stopped, stopped responding or failed to start. A record that
  carries no start token is shown the same way on any system.
- The "journal off behaves as before" proof compares the loop with golden files made
  from the code before the journal existed (`tests/golden/`). It covers the
  legacy-loop scenarios listed in `tests/golden_off_scenarios.py` (no commit gate):
  final files, log lines, exceptions, waits and heartbeat counts after the volatile
  values are normalised. It is not exhaustive byte equality for every path, and
  two deliberate breaks at rarely used sites survived it (skipping one
  attempt-ledger cleanup, and skipping one heartbeat stamp): known coverage limits.
- The journal writes nothing to the wrapper's own log. A journal that did not
  start, or a fault while writing, shows only in the journal's own status
  record. `agenttalk status` and `agenttalk doctor` show the journal's state label
  (for example `off (start_failed)` or `writer not responding`); the fault counts
  and the last fault live in the journal's status record.

**Not recorded.** Plain `wrap` (without `--loop`), `--one-shot` reviewers and the
proactive sweeps of a `--lead-loop` wrapper are not journaled, and `status` says
so. The journal never holds prompt or reply text, file paths, command lines,
environment values, secrets, model names or error text.

**What `status` and `doctor` show** (only once the project has a journal folder;
otherwise their output is unchanged), computed from live facts, never from the
newest file alone: a writer counts as running only when its process is
still running (an affirmative answer from the system; an exited child whose parent
still holds its handle does not count) **and** its start token matches the record
(an unknown or unreadable answer is "not running"), and among several records the current wrapper's own come first:
`on (loop)`, `on (loop); cadence turns unmanaged`,
`writer not responding`, `off`, `off (start_failed)`, `off (start_timeout)`,
`ended`, `unmanaged (one_shot)` and `unmanaged (plain)`; where no start token can
be read (see the limits above), `unknown (no process identity on this platform)`
instead of any label that would need one. A file left by an
earlier run never changes the label of a running wrapper. A journal that never
started at all (for example because its thread could not be created) is shown
as `off (start_failed)` or `off (start_timeout)` through the wrapper's own
health record, so it needs no extra file write. That warning names the wrapper
that wrote it (its process id and a digest of its start token), and is shown only
while that same wrapper is the live one; a replacement wrapper never shows its
predecessor's failure.

**Reading it.** `agenttalk.turn_events` has the reader (`read_streams`,
`list_segments`, `read_segment`, `iter_records`) and the closed record checker
(`validate_event`). The schema version is 1; a reader refuses another version.
`iter_records` moves its cursor past a record only when it hands that record
over, so a reader that stops early (or reads in batches) resumes at the first
record it has not received.

### Where agenttalk keeps files

The coordination store (messages, the roster, cursors, thread state and
archives) is in the project's `.agenttalk/` folder. A few things live
outside it, in per-user folders. By default they are:

- **signing keys**, if you turned on message signing with
  `agenttalk hmac-init`: `%LOCALAPPDATA%\agenttalk\keys\` on Windows,
  `$XDG_CONFIG_HOME/agenttalk/keys/` (default `~/.config`) elsewhere.
  Without the key, signed messages cannot be verified;
- **backups** made with `agenttalk backup`: `agenttalk\recovery\` in the
  same per-user folder, unless `AGENTTALK_RECOVERY_DIR` points elsewhere;
- **the supervisor's wrapper logs**: `%LOCALAPPDATA%\agenttalk\wrapper-logs\`
  on Windows, `$XDG_STATE_HOME/agenttalk/wrapper-logs/` (default
  `~/.local/state`) elsewhere;
- **the turn journal**, a record of what each agent's turns did, written
  only when it is switched on: `%LOCALAPPDATA%\agenttalk\turn-events\` on
  Windows, `$XDG_STATE_HOME/agenttalk/turn-events/` (default
  `~/.local/state`) elsewhere, one folder per project and one subfolder per
  agent, unless `AGENTTALK_TURN_EVENTS_DIR` points elsewhere;
- **the managed gateway's secrets** (its API key and tokens), **its
  `install.json` and its spend ledger**, if you use the gateway:
  `agenttalk-ovh\` and `agenttalk-ovh-spend\` under `LOCALAPPDATA` when that
  variable is set, otherwise under `~/.local/share`, on every system;
- **Codex settings**: `agenttalk codex-config --enable` adds a block for
  this project to `~/.codex/config.toml`.

To move them: `AGENTTALK_HMAC_KEY_FILE` sets the signing key file,
`AGENTTALK_RECOVERY_DIR` sets the backup folder,
`AGENTTALK_TURN_EVENTS_DIR` sets the turn journal folder, and
`agenttalk codex-config --config-path` uses a different Codex settings
file.

`agenttalk backup` copies only the coordination store; none of the
per-user items above are in it. Restoring the gateway needs its whole
per-user folders: the secrets (API key, front token, internal token),
`install.json` and the spend ledger, because the gateway checks the front
token against the ledger when it starts. Deleting the project folder does
not remove the per-user items above.

### Windows notes

- The generated supervisor (`supervisor.ps1`) runs on Windows only; a
  monitor for Linux and macOS is a follow-up ([#356](https://github.com/zoolok17/agenttalk/issues/356)). The project's tests,
  which cover `wrap`, run on all three systems; the managed gateway's
  background service uses Windows Task Scheduler or a systemd user service,
  so it cannot be installed on macOS. The wrapper gives a
  wrapped Codex seat its reply instructions in PowerShell form by default, so
  a wrapped Codex seat whose shell is bash or zsh needs `"reply_shell": "bash"`
  in `.agenttalk/supervisor.json`, for that seat or for all seats; the wrapper
  reads that file even when the supervisor does not run. A Codex assistant you
  start yourself does not use this setting.
- The supervisor requires **PowerShell Core 7+** (7.4+ recommended;
  7.0–7.3 runs with an end-of-life warning; Windows PowerShell 5.1 is
  refused). Select a specific `pwsh.exe` explicitly with `agenttalk
  supervise --select-pwsh --pwsh "<absolute path>"` if you need a
  portable or nonstandard host.
- Inline `-m "..."` bodies are fragile for multi-line text,
  apostrophes, backslashes, and Windows paths. Prefer `--file <path>`,
  or pipe a PowerShell here-string to `--file -`, for `send`, `reply`,
  `propose`, and `broadcast` bodies.
- Supervised agents launch hidden by default on Windows so an
  unattended fleet doesn't open one console per agent. Set
  `"window_style": "minimized"` or `"normal"` in `supervisor.json`
  (top-level or per-agent) to change that.

### FAQ

**Why did my env var not take effect in the next tool call?**
Env vars set *inside* an LLM tool call (for example `$env:AGENTTALK_SELF
= 'claude-a'` in one PowerShell call) may not persist into the next
tool call, since each call can be a fresh shell process. Set env vars
in the parent shell/profile instead, or pass explicit
`--from`/`--to`/`--for` flags. The bundled skills already resolve
identity inside each tool call, so this mostly matters for
hand-rolled automation.

**Can two windows listen as the same agent?**
Not supported. Each agent is meant to run in exactly one consuming
window per store; a duplicate can lose cursor/thread-state updates or
produce conflicting replies. `wait` warns (advisory, never blocking)
when it detects a live duplicate waiter, and `agenttalk doctor` reports
the current waiter's PID. Pass `--refuse-stacked-wait` to make that a
hard exit 6 instead of a warning.

**Does compaction lose history?**
`agenttalk compact` never archives anything unread, protected, or
still-open — live state is byte-for-byte unaffected. What it does
trade away is *closed* thread history: once an old, resolved request's
messages are cold-archived, `check --to-request <that-id>` can return
unknown (exit 4) rather than reconstructing the old verdict. That's a
deliberate, fail-closed boundary (unknown, never wrong), not silent
data loss.

**What happens if two machines' clocks disagree?**
Message ids are timestamp-prefixed and delivery order is a lexical
compare of ids. A well-formed but future-dated id from a fast machine's
clock skew can mis-order or hide later messages from a slower one.
Keep synced machines' clocks in agreement (NTP) if you sync one
`.agenttalk/` store across machines.

**Exit codes** (stable across releases — safe for skills and external
automation to depend on):

| Code | Meaning |
| --- | --- |
| `0` | Success. For `wait`: a message was received. |
| `1` | `wait` timeout — treat as "keep waiting," not an error. A few other commands also return `1` for a not-found/blocked case (e.g. `attention show` on an unknown item, `checkpoint show` with no checkpoint, `wrap` on a config-blocked launch) or an uncaught error — check the command's own output to tell those apart from a `wait` timeout. |
| `2` | Usage error: bad/missing identity, unsafe name, corrupt config, missing `.agenttalk/`, or a `serve`/`dashboard` bind failure. |
| `3` | The request was superseded/rescinded, or `close`/`gate`/`lane` check is `HOLD`. |
| `4` | Unknown request id (`check`). |
| `5` | Partial broadcast fan-out — see the delivered/missed manifest, or `--resume`. |
| `6` | `wait` duplicate-wait class: `--refuse-stacked-wait` refusal, or superseded by a newer same-thread waiter. |
| `130` | `SIGINT` (Ctrl-C). |

### Versioning and releases

agenttalk follows [Semantic Versioning](https://semver.org/) and keeps
a [Keep a Changelog](https://keepachangelog.com/)-formatted
`CHANGELOG.md`. Install a specific tag (`@v0.98.0`, or whatever the
current release is) rather than a branch, since the CLI surface and
message schema can change between releases. `agenttalk dev-gate
--profile release` is the release-evidence gate itself: it builds the
package, runs tests against both source and the built wheel per
supported Python minor, and (via `--ci-leg`/`--aggregate`) combines
per-CI-leg evidence into one authoritative verdict that `close`/`gate`
can consume.

### Further reading

This reference deliberately stays shallow. For more:

- [docs/AGENTTALK-NEW-USER-MANUAL.md](docs/AGENTTALK-NEW-USER-MANUAL.md) — concept-first onboarding.
- [docs/USER-MANUAL.md](docs/USER-MANUAL.md) — operator-facing procedures and examples.
- [docs/AGENT-MANUAL.md](docs/AGENT-MANUAL.md) — role-keyed operating guide for agents.
- [docs/DESIGN.md](docs/DESIGN.md) — architecture, rationale, and decision history.
- [docs/ASSURANCE.md](docs/ASSURANCE.md) — release attestation and gate evidence in depth.
- [docs/supervisor-tutorial.md](docs/supervisor-tutorial.md) — full supervisor/wrapper walkthrough, including migrating an existing project in and out of supervision.
- [docs/BACKUP.md](docs/BACKUP.md) — what `agenttalk backup` guarantees and does not.
- [CHANGELOG.md](CHANGELOG.md) — release history.
- [SECURITY.md](SECURITY.md) — security posture and trust model.
- [docs/README-ARCHIVE-2026-09.md](docs/README-ARCHIVE-2026-09.md) — the pre-rewrite README, kept for reference (superseded, not maintained).
