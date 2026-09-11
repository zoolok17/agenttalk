# agenttalk

1. [Quick intro](#1-quick-intro)
2. [Quick setup](#2-quick-setup)
3. [Use cases](#3-use-cases)
4. [In depth: how a migration works](#4-in-depth-how-a-migration-works)
5. [Technical reference and FAQ](#5-technical-reference-and-faq)

---

## 1. Quick intro

agenttalk is a small, file-backed message bus that lets coding-agent
CLIs — Claude Code and Codex, a pair or a named team — talk to each
other directly and work on the same repo. There is no daemon and no
server: every message is a JSON file under a project-local
`.agenttalk/` directory, and each CLI runs in its own terminal window
so you watch the full conversation as it happens.

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
that a reviewer with a different training lineage is less likely to
share the first agent's blind spots. The bus is symmetric; which agent
plays which part on a given task is your call.

### What grows around that core

The two-agent handoff is the whole essence, and it stays simple:
`send`/`reply`, or the `/agenttalk.handoff` skill, for one agent to
hand work to another and block on the answer. Everything else is
opt-in, added to support real multi-agent work once a pair grows into
a team or runs unattended:

- **Named teams** — roles, groups, a lead/operator-liaison identity,
  and `broadcast` fan-out to a role or group.
- **Operator safety** — supersede/rescind so a stale request can't
  quietly get actioned, pre-action `check`, and epoch barriers.
- **24/7 supervision** — a background monitor that restarts agents
  across provider outages or stuck turns, and a progress wrapper
  (`agenttalk wrap`) that resumes the agent's actual session context
  rather than starting the turn over.
- **Shared ownership** — a `domain` registry mapping repo areas to
  owners, reviewers, and curators, with a scoped `lane` deliver-gate
  built on top of it.
- **Durable memory** — an `onboarding` ledger for what the team learned
  about a codebase before touching it, and a `knowledge` layer for
  pointer notes and lessons that outlive any one session.
- **Assurance** — a `gate` HOLD/GO state plus typed review evidence, so
  a milestone can't close on the strength of an unreviewed claim.
- **A read-only dashboard** — a local web console (`agenttalk serve` /
  `agenttalk dashboard`) for watching roster, threads, and obligations
  without joining the bus yourself.

### Local-first, no egress

agenttalk itself makes no network calls. The bus is files on disk; the
only network traffic is whatever the underlying agent CLI (Claude
Code, Codex) already makes to its own provider. The bundled dashboard
binds to loopback only and has no flag to expose it — reach it from
another machine over an SSH tunnel if you need to, not by opening the
port.

### What agenttalk is not

- **Not a model.** agenttalk doesn't call any model API itself and has
  no opinion on which model a CLI uses — it only moves messages between
  whatever agent CLIs you start. The intelligence is entirely in the
  agents; the bus just lets them talk.
- **Not an IDE plugin.** There's no editor integration to install.
  agenttalk is a CLI-level bus: it works with whatever terminal or
  editor-embedded terminal you already run your agent CLIs in.
- **Not a hosted service.** No account, no server to sign up for, no
  cloud component. Everything lives in your project's `.agenttalk/`
  directory on your own machine.
- **Not a task queue.** There's no central scheduler deciding what
  runs next; agents decide what to do and message each other about it.
  If you want an explicit "what's next" driver, pair agenttalk with a
  workflow tool such as spec-kitty — agenttalk carries the wake
  signal, the workflow tool remains the source of truth for state.
- **Not a replacement for git.** Nothing here manages branches, merges,
  or history. `lane` and `domain` gate *who may deliver what*, using
  git diffs as evidence; they don't perform the merge.
- **Not a multi-machine system.** Both agents are expected to share one
  project directory on one machine (or a directory synced by a
  mechanism you already trust). There's no transport, no server
  process, and no attempt to solve distributed consensus.

---

## 2. Quick setup

### Install (tag-pinned)

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.87.0"
agenttalk install-skills          # installs bus skills + the dev-discipline devkit
```

Pin to a released tag (`@v0.87.0` above, or whatever the current
release is) rather than a branch — the CLI surface and message schema
can change between releases, and a tag keeps every agent in a project
talking the same protocol version.

`agenttalk install-skills` writes Claude Code's bus commands under
`~/.claude/commands` and Codex's under `~/.codex/skills` by default;
pass `--claude-only` or `--codex-only` to install just one side, or
`--dry-run` to preview without writing.

### Initialize a project

Once per project, from the project root, with the actual names you'll
use in each terminal (not the generic `claude`/`codex` default — see
the next section for why):

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
explicit `--from <name>` on every bus command it runs) — without one of
the two, its commands silently fall back to a default identity instead
of failing loudly, which can route messages to or from the wrong agent
with no error. `agenttalk init` prints this same reminder after it
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
lands rather than after. If you're driving work from a spec/plan tool
such as spec-kitty, agenttalk's `sk-loop` skills (`/agenttalk.sk-loop`
for Claude, `$agenttalk-sk-loop` for Codex) call `spec-kitty next
--agent <self>` to decide what's next, do the work, and send a small
wake message so the peer reacts instantly — spec-kitty stays the
source of truth for state, agenttalk is just the wake signal. Without
a spec tool, wire up roles directly:

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
restart with their session context intact instead of starting over.
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

### Command reference by category

Every command below is a real, currently-shipping subcommand
(`python -m agenttalk <cmd> --help`); flags shown are the ones most
relevant day to day, not exhaustive lists — run `--help` on any
command for the full set.

**Messaging**

| Command | What it does |
| --- | --- |
| `send` | Point-to-point message. `--kind`, `--to`, `--await-reply`. |
| `reply` | Answer the latest (or a specific, via `--to-id`/`--to-request`) received message. `--na` for a non-substantive close. |
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
| `close {open,ack,draft,counter,check,publish,reopen,list,show}` | Aggregates gates + typed review evidence into one milestone/release verdict. |
| `close signoffs {plan,apply,override}` | Derives specialist sign-off routing by risk class. |
| `check` | Pre-action HOLD/GO check, optionally `--gates`-aware. |
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
| `wrap` | Structured-stream adapter around a CLI child: visibility, working-turn heartbeat, degraded-output detection. `--loop` for supervised long-running mode, `--one-shot` for a single turn. |
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
| `scratch root` | Resolve/create `<scratch_root>/<agent>[/<task>]`. |
| `janitor` | Report (default) or `--apply` cleanup: WIP-commits dirty **registered worktrees** on their own branch (never the default branch, never a detached HEAD), removes allow-listed scratch paths, and prunes stale worktree registrations; `--keep-days`. |
| `doctor` | Health check; `--json` for automation. |
| `reset` | Clear active bus state; `--archive` preserves it instead of deleting. |
| `capacity {show,refresh}` | Publish/read context-window budget so a team can see who's near compaction. |
| `codex-config` | `--enable`/`--disable`/`--status` for Codex sandbox approval settings. |
| `install-skills` | Install bus skills (and the dev-discipline devkit) for Claude and/or Codex. |
| `hmac-init` | Provision HMAC signing material. |
| `gateway` | `{init,start,stop,status,...}` — multi-agent process gateway lifecycle. |

**Dashboards**

| Command | What it does |
| --- | --- |
| `serve` | Single-project read-only web view. Loopback-only (`127.0.0.1`/`::1`/`localhost`); no flag exposes it beyond that. |
| `dashboard` | Same server, multi-root obligation view under `/dashboard`. `--store` is repeatable. |

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

### Windows notes

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
`CHANGELOG.md`. Install a specific tag (`@v0.87.0`, or whatever the
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
- [CHANGELOG.md](CHANGELOG.md) — release history.
- [SECURITY.md](SECURITY.md) — security posture and trust model.
- [docs/README-ARCHIVE-2026-09.md](docs/README-ARCHIVE-2026-09.md) — the pre-rewrite README, kept for reference (superseded, not maintained).
