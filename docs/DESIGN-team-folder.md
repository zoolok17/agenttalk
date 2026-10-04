# Design: keep each team's work inside its own folder (team folder, stage 1)

Status: proposed. This is the design stage of #336; nothing in it is built yet. Where
it describes how agenttalk works today, that matches the code at the commit it ships
with, and the canary in the appendix measured it on Windows.

Audience: the operator who runs agenttalk teams on a machine, and the developers who
will build stage 1.

## In plain words

An agenttalk team leaves files in many places on a machine: the user's temp folder,
download caches shared with every other program, stray test folders and old run
folders that nothing ever removes. This design gives each team one folder, the
**team root**, with fixed places inside it for lasting work, throwaway files and
caches. Every seat the team starts gets its temporary files and caches pointed there
before it starts. Because putting files in one place does not stop a disk from
filling, the design also adds size limits, low-space warnings and a clean-up policy
with a named owner, all through the existing clean-up command. The clean-up removes
only what it can show is safe to remove, and keeps everything else. A **containment
report** says, for each location, whether it is only configured, actually seen,
unknown or enforced, and it never claims more than it saw.

This is not a security boundary. Every team still runs as the same Windows user, so
any program a seat starts can still write anywhere that user can. Moving the folders
of teams that already exist is a separate, later operation. Stage 1 moves only one
small thing: the build tools that one host's seats borrow from another project's
folder go into the team's own folder.

What this document decides: the team root's layout and settings, which variables each
kind of child process gets and how they are set before a process starts, which files
the clean-up may remove and on what evidence, how limits, warnings and the trash work
and who owns them, the four states of the report, the declared exceptions, and what
stage 1 builds, with acceptance cases. What it leaves open is listed under "Open
questions".

## Why

On 2026-10-04 one host was down to about 1% free space on its system drive. Old work
was spread over the user's temp folder, a shared scratch folder with 2.8 million
files, and 18 stray test folders in the drive root. On a second host, two teams share
one Windows user, so they share its temp folder, the pip and npm download caches and
the AI tools' home folders. A clean-up there cannot tell whose files are whose.

Measuring for this design also found that the dev gate never removes its run folders,
and that the clean-up command cannot see them. That is tracked and fixed separately,
in #338.

The operator asked for all team work to stay in the team's folder, with exceptions
only where needed and never for general work or the message bus. The operator also
decided two things up front. First, the AI tools' home folders (settings and login)
stay in the user's home as a declared exception. Second, one Windows user per
machine, for now.

An independent challenge (`ch-ac2ae695-441c-46b2-8605-747c3b040474`, verdict
"reshape", accepted) set the shape this design follows: stage 1 is the team root with
limits and an owned clean-up policy, checked at every child boundary, with an honest
report; moving existing folders comes later.

## The team root

### Layout

| Folder | What goes there | What the clean-up may do |
|---|---|---|
| `projects/` | The team's agenttalk project folders: each holds its `.agenttalk/` bus and its checkout. | Nothing. |
| `work/` | Work a seat wants to keep: review worktrees, reports, saved probe results. | Nothing. |
| `tools/` | Programs the seats need on their path that are not installed for the whole machine, such as a build tool, a Java runtime or Node. | Nothing. |
| `scratch/<seat>/<task>/` | Each seat's throwaway work (`AGENTTALK_SCRATCH`). | Only folders marked disposable, under the rules below. Everything else is kept and reported. |
| `tmp/<seat>/<run>/` | One folder per wrapper run: that run's temporary files (`TEMP`, `TMP`, `TMPDIR`) and those of everything it starts. | Only folders of runs that have provably ended, under the rules below. |
| `cache/` | Download and build caches: pip, npm, compiled Python files, Maven. | Only the tool caches listed below, and only by size. |
| `state/` | agenttalk's own per-team records: wrapper logs, the turn journal. | Nothing. Each writer limits its own size. |
| `trash/` | What the clean-up removed, kept for a few days so it can be restored. | Deleted after a set age; deleted early only with permission. |

"Inside the team root" never means "safe to delete". The clean-up acts only on the
folders the table names, only on the evidence described under "What the clean-up may
remove", and keeps anything whose safety it cannot show.

### Settings

The team root is declared in each of the team's projects, in
`.agenttalk/config.json`, under a new `team` key:

```json
{
  "team": {
    "root": "D:/Teams/example",
    "strict": true,
    "budgets": {
      "total": {"bytes": "80 GiB"},
      "tmp": {"bytes": "20 GiB", "files": 500000},
      "scratch": {"bytes": "20 GiB", "files": 500000},
      "cache": {"bytes": "30 GiB"},
      "trash": {"bytes": "10 GiB"},
      "state": {"bytes": "2 GiB", "warn_only": true}
    },
    "low_space": {"warn_free_percent": 10, "stop_free_percent": 3},
    "retention": {"tmp_keep_days": 2, "scratch_keep_days": 7, "trash_keep_days": 7},
    "owner": "example-lead",
    "exceptions": []
  }
}
```

(Partial example of the proposed format, not runnable yet. The numbers are
placeholders for the operator to set, not recommendations.)

- **`strict`** is the switch the operator turns on per team. With it on, a setting
  that does not hold refuses instead of falling back. Examples: a root that is
  missing, relative, unwritable or not a folder; a subfolder that a link or junction
  takes outside the root; a `scratch.root` outside the root; an exception without a
  reason. `agenttalk scratch root` and the clean-up exit with an error, and the
  supervisor does not start the team's seats. Today an unusable scratch setting
  silently falls back to the shared sibling `atk-scratch` folder; strict mode never
  does.
- **Without `team`**, or with `strict` off, everything behaves as it does today,
  including today's clean-up rules (see "Open questions").
- The existing `scratch` settings stay. In a team, the scratch root defaults to
  `<team root>/scratch`, and `scratch.root` must lie inside the team root.

## Where each kind of child gets its locations

A seat is a chain of processes, and the variables travel down the chain. The
challenge's point stands: changing the launcher alone is not enough, because several
links in the chain build their own environment.

### The routed variables

All inside the team root:

| Variable | Points to | Who reads it |
|---|---|---|
| `TEMP`, `TMP` | `tmp/<seat>/<run>/` | Windows programs, Python, Node |
| `TMPDIR` | `tmp/<seat>/<run>/` | Python everywhere; many programs on Linux and macOS |
| `PIP_CACHE_DIR` | `cache/pip/` | pip |
| `npm_config_cache` | `cache/npm/` | npm |
| `PYTHONPYCACHEPREFIX` | `cache/pycache/` | Python's compiled files |
| `XDG_CACHE_HOME` | `cache/xdg/` | Linux and macOS tools that follow the XDG rules |
| `AGENTTALK_SCRATCH` | `scratch/<seat>/` | seats (already set today from the scratch setting) |
| `AGENTTALK_TURN_EVENTS_DIR` | `state/turn-events/` | the turn journal |
| (new) wrapper log folder | `state/wrapper-logs/` | the supervisor's wrapper logs |
| `JAVA_HOME`, `MAVEN_HOME` and the tools' `bin` folders first on `PATH` | `tools/` | Java and Maven builds, Node tests (set by the host's launcher; see "The desktop migration step") |
| Maven's download cache (`-Dmaven.repo.local` in `MAVEN_OPTS`) | `cache/maven/` | Maven, which otherwise uses `.m2` in the user's home |

What stage 1 does **not** move: `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA`.
Moving them would also move the AI tools' logins, agenttalk's signing keys and the
gateway's secrets, which the operator decided stay per user (see "Declared
exceptions").

### 1. Supervisor to seat: routing is set before the process starts

Python fixes its compiled-file folder when it starts, and its temp module remembers
the temp folder it chose the first time it is asked. Changing the variables inside a
process that is already running is therefore too late. The canary's reviewer
measured this on both Python versions. So the variables must already be in the
environment when the wrapper's interpreter starts.

- **Supervisor launches and restarts.** For each launch, the supervisor creates a
  fresh run folder `tmp/<seat>/<run>/` and writes an owner record into it (the seat
  and the run). It then applies the routed variables. Restarts, including a
  requested restart and a recovery relaunch, go through the same launch path, so
  each gets its own run folder with the same routing.
- **Precedence.** Today the supervisor applies its own entries first, then the seat's
  own `env` setting (or, for a recovery launch, the recovery environment). Stage 1
  applies the routed set **last**, and the routed names are reserved: in strict mode a seat's `env` or a
  recovery environment that sets any of them is refused, at config validation and at
  launch. A gateway-backed seat already refuses a literal `env` setting; the routed
  set is computed, not literal, so that refusal stays as it is.
- **Hand starts.** A new command, `agenttalk team run --for <seat> -- <command>`,
  creates the run folder, sets the routed variables and starts the command. It is the
  supported way to start a seat by hand.
- **The wrapper checks itself first.** The first thing the wrapper does, before it
  imports anything that uses temp files, is check the routing in its own
  environment:
  - its temp variables name a run folder of this team whose owner record names this
    seat;
  - Python's compiled-file folder equals the team's.

  If the check passes, the wrapper adds its process id and process start token to the
  owner record. In strict mode a failed check refuses to start and names the
  supported command; with strict off it records a warning. The wrapper does not
  restart itself with a corrected environment: on Windows that would give it a new
  process id, and the supervisor tracks the one it started.
- **What can be written before the check.** Starting Python and importing the
  wrapper's entry modules can write compiled files next to the installed agenttalk
  runtime, when the routing is missing (a hand start without `team run`). Those files
  belong to the installed-program exception. Usually there are none, because pip
  compiles them at install. No temp file is created before the check: it reads only
  environment variables and the owner record.

### 2. Wrapper to an ordinary model child

The wrapper passes its whole environment to the model's command-line tool, minus a
few of its own markers. Whatever the wrapper has, the tool and everything the tool
starts inherit.

Canary (Windows): with the routed variables set, the child's temp folder, a file it
wrote and a compiled file it wrote were inside the team root. pip's resolved cache
folder was too. Without the variables, they resolved to the user's temp folder and
the user's home.

Not observed: the real AI tools' own use of temp and caches, because no paid turn
ran. Until a seat's own observation exists, the report says "unknown".

### 3. Wrapper to a gateway-backed child

This child gets a deliberately small environment, because a paid outside worker must
not learn the operator's real home:
- It keeps `PATH`, the Windows system folders, `TEMP`, `TMP`, two Python text
  settings and every `AGENTTALK_` variable, and drops everything else.
- It sets `HOME` and `USERPROFILE` to the project folder, and `LOCALAPPDATA` and
  `APPDATA` to folders below it.

Canary (Windows):
- Its temp folder followed the team's `TEMP`, and so did a file it wrote.
- pip's cache folder resolved into the project folder: inside the team root, but
  mixed in with the bus and the checkout.
- A compiled file for installed code would go next to the installed program, because
  the compiled-file setting is dropped.

`TMPDIR` does not reach this child. That alone does not decide where it puts
temporary files:
- Python checks `TEMP` and `TMP` too, and the child keeps both; the canary measured
  this for Python on Windows.
- Node's runtime library documents that it checks `TMP` and `TEMP` after `TMPDIR`;
  this was not measured.
- A program that reads only `TMPDIR` would fall back to its own default. Whether this
  child runs such a program, and anything on Linux and macOS, is unmeasured.

Stage 1: let `TMPDIR`, `PIP_CACHE_DIR`, `npm_config_cache`, `PYTHONPYCACHEPREFIX` and
`XDG_CACHE_HOME` through, each only when its value lies inside the team root. They
name folders inside the same team root as `TEMP`, which the child already gets.

### 4. The dev gate and its tools

The dev gate builds its own environment from a short list:
- it sets `TEMP`, `TMP` and `TMPDIR` to its own run folder;
- it turns pip's cache off;
- it passes on `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA`;
- it puts the run folder inside its own process's temp folder, unless `--temp-root`
  says otherwise.

Canary (Windows), running a test the way the gate runs pytest:

- With the team's `TEMP`, the run folder, the test's temp folder, a file it wrote, a
  compiled file it wrote and pytest's own cache were all inside the team's temp
  folder.
- The gate runs Python in isolated mode, which ignores `PYTHONDONTWRITEBYTECODE` and
  `PYTHONPYCACHEPREFIX`. So compiled files for the exported code are written next to
  it, inside the run folder. Compiled files for installed code would go next to the
  installed program.
- Tests still see the user's real home folders. Which tests write there is unknown.

Stage 1: when a team root is set, the gate puts its run folder under the seat's run
folder itself, not only through `TEMP`, so a gate run started from an ordinary shell
lands there too. Removing old gate run folders is #338.

### 5. The comprehension worker

The worker gets a fixed short list:
- It keeps `PATH`, the Windows system folders, the home variables, `TEMP`, `TMP` and
  `TMPDIR`, and sets `PYTHONPATH` to the agenttalk it runs.
- It drops `PYTHONPYCACHEPREFIX`, `PYTHONDONTWRITEBYTECODE` and the cache variables.
- It starts Python with `-s -S`, which skip the user and site folders but do not stop
  compiled files from being written.

Canary (Windows):
- Its temp folder and a file it wrote followed the team's `TEMP`.
- A compiled file for installed code, such as the agenttalk it imports, would go next
  to the installed program, outside the team root.
- pip's cache folder resolved to the user's home. The worker runs no pip.

Stage 1 policy: start the worker with `-B` as well, so it writes no compiled files at
all. No new path crosses the worker's privacy list. The worker then compiles in
memory each time it starts, which costs a little time per scan.

### 6. Other children agenttalk starts

- **Packaging checks:** they use Python's temporary folders, which follow the seat.
- **The gateway service:** a declared exception (below). It runs from its own
  scheduled task, with that task's temp folder.

## What the clean-up may remove

The clean-up removes only what it can show is safe. When it cannot show that,
it keeps the entry and reports it. Age alone never shows safety, and neither does a
folder's name.

### Temp run folders: `tmp/<seat>/<run>/`

A run folder may go to the trash only when all of these hold:

1. **Its owner record is readable.** It names this team's seat and run.
2. **Its run has ended.** The recorded process is gone, or its process start token no
   longer matches. If agenttalk cannot read a process's identity on this system, or
   the record has no token, the run counts as possibly running and the folder is
   kept.
3. **Nothing in it was touched recently.** The newest file anywhere inside is older
   than `tmp_keep_days`, measured over the whole tree, not the folder's own time.
4. **No link takes it outside the team root** (see "Links").

Anything directly in `tmp/` that is not a run folder has no known owner. It is kept
and reported.

Temporary files are throwaway by contract: a program that needs a file after its run
has ended must keep it in `scratch/` or `work/`. The age rule in point 3 is a margin
for a process that outlives its run, not the proof of safety. The proof is points 1
and 2. Even then the folder goes to the trash, where it can be restored, not straight
to deletion.

### Scratch task folders: `scratch/<seat>/<task>/`

These are kept unless they are marked disposable. A folder is marked when it is
created as throwaway, for example by `agenttalk scratch root --for <seat> --task <task>
--disposable`, which writes a marker naming its seat and task. A marked folder may go
to the trash only when all of these hold:

1. its task's request is closed on the bus;
2. the newest file anywhere inside is older than `scratch_keep_days`;
3. no link takes it outside the team root;
4. it holds no worktree that the worktree rules below would keep.

An unmarked folder is never removed automatically. When scratch is over its budget,
the owner gets an attention item listing the largest unmarked folders, for a person
or the owning seat to decide. Today's clean-up removes any scratch task folder older
than its keep days; in a team, these rules replace that.

### Worktrees

The existing protections stay unchanged:
- a worktree with any tracked or untracked change gets a WIP commit on its own branch,
  or is refused;
- a worktree on a default branch, or on a detached commit, is refused;
- a worktree whose commit no branch, tag or remote-tracking ref holds is refused.

New: files git ignores. Today a worktree whose only extra files are ignored counts as
clean, so it can be removed. In a team, an ignored file counts as rebuildable only
with evidence:

- it is inside a folder holding a valid `CACHEDIR.TAG`, the standard marker by which
  a tool declares a folder to be its cache (pytest, ruff and mypy write one); or
- it is a compiled Python file in a `__pycache__` folder, and the source file it was
  compiled from is present in the worktree.

Any other ignored file, including anything under `build/` or `dist/`, is of unknown
value. A worktree holding one is kept and reported as "holds ignored files of unknown
value".

### Caches: `cache/`

Only caches that their tools define as caches are trimmed, oldest first, and only
when `cache/` is over its budget:
- pip's and npm's caches: both tools document them as caches they refill on demand;
- the compiled Python files: Python rebuilds them from source;
- folders under `cache/xdg/` with a valid `CACHEDIR.TAG`.

Maven's local repository is not trimmed: besides downloads it holds artifacts built
locally with `mvn install`, which may have no other copy. Over budget, it gives a
warning only. Cache entries are deleted directly, not moved to the trash, because a
cache holds nothing that cannot be fetched or rebuilt. A file a running tool holds
open is skipped.

### Every action is checked again just before it happens

The report finds candidates. Applying it repeats every check above for each entry
immediately before acting on it:
- the owner and liveness;
- the newest-file age;
- the git state;
- links and the real location.

If anything changed since the report, that entry is skipped and reported as "changed
since the report".

### Links

The clean-up never follows a link or junction while walking a folder. Before every
action that changes anything, it resolves the entry's real location and refuses, and
reports, if that lies outside the team root:
- a WIP commit;
- a worktree prune;
- a move to the trash;
- a deletion.

It prunes a worktree registration only when the registration's recorded path lies
inside the team root. A link found inside a folder that is being removed is removed
as a link; its target is never touched.

## Limits, warnings and the trash

Putting files together does not stop a disk from filling. Stage 1 adds:

- **Free-space warnings.** `doctor`, the console and the supervisor check the free
  space on the drive that holds the team root and on the system drive, where the
  user's home and temp live.
  - Below `warn_free_percent`, it is an attention item for the team's owner.
  - Below `stop_free_percent`, the dev gate refuses to start, because it is the
    largest writer, and the supervisor raises an urgent item.
  - Running seats are never stopped for this: stopping them would lose work.
- **Budgets.** Each folder in the settings has a limit, and `total` covers the team
  root as a whole, the trash included. The clean-up report measures them with a
  bounded scan. A scan that hits its time or entry limit, or cannot read a folder,
  says "size unknown", never a guess and never zero. Over budget is an attention item.
- **The trash frees no space.** Moving files to `trash/` on the same drive does not
  give space back. So:
  - The trash has its own budget and keep days, and counts toward the total.
  - Every clean-up result says two numbers separately: space freed, and space moved
    to the trash and not freed.
  - When space is low and the trash holds files, the clean-up says so plainly, for
    example: "12 GB moved to the trash; no space was freed; `--purge-trash` would free
    12 GB".
  - Freeing that space early needs permission from the team's owner or the operator:
    `--purge-trash` deletes trash entries oldest first, and `--no-trash` deletes
    newly eligible entries without the trash step.
  - Both only shorten the restore window for entries that already passed every check
    above. Neither ever touches an entry that failed one.
  - Without that permission, nothing more happens, and the result says no space was
    freed.
- **The `state/` folder** has a warning-only budget. Its writers already limit
  themselves:
  - the supervisor keeps a set number of wrapper-log generations per seat, and each
    log is a bounded ring;
  - the turn journal's writer keeps each seat's journal under 64 MiB, removing its
    oldest files first, and keeps the newest four older status files;
  - the journal's list of runs (`streams.jsonl`) grows by one short line per run.

  The clean-up never touches `state/`. Stage 1 adds no new state retention.

**Who owns it.** The operator sets the policy in the `team` setting. The team's
`owner`, normally its lead seat, runs the clean-up report on its routine (daily),
applies it when a budget is exceeded or at a quiet time, and is the one who can give
the trash permissions. `doctor` and the console show the state to everyone. There is
one cleaner, the existing `agenttalk janitor`; stage 1 adds no second one.

## The containment report

`agenttalk doctor` and the console gain a containment section. Each location gets
exactly one of four states:

| State | Meaning | Example |
|---|---|---|
| configured | agenttalk's settings or records say this location points inside the team root. No write there has been seen. | A seat's `PIP_CACHE_DIR` in its launch settings; the paths a wrapper resolved at start. |
| observed | A specific write or file was seen at a stated time, within a stated scope. | The wrapper's start probe wrote its file inside its run folder at 09:14; a scan of `tmp/` completed at 09:20 found 3,210 files. |
| unknown | Nothing tells us, or the evidence is too old, or a scan failed. | The AI tool's own caches; tests writing to the real home folders during a gate run; anything on Linux or macOS until measured. |
| enforced | agenttalk itself refuses the alternative. | Strict mode refuses to start a wrapper whose routing is missing; the clean-up refuses to act outside the team root. |

Rules:

- **Configuration is never an observation.** Resolved paths, settings and start
  records are "configured", however exact they are.
- **An observation names its write, time and scope.** Two sources:
  - the wrapper's start probe: right after its routing check, the wrapper creates and
    removes one small file through Python's temp module, and records where it landed
    and when;
  - the clean-up report's scans, each recording when it ran, which folders it
    covered, and whether it finished.

  An observation older than a set age (for example a day) is shown as "unknown, last
  observed at <time>".
- **Found is not attributed.** A scan of outside places reports what it found. It
  attributes an entry to this team only when the entry carries agenttalk's own
  naming for this team: for example a wrapper-log folder named by this project's id,
  or a run folder with this team's run id. Anything else, including same-user names
  such as `pytest-of-<user>`, is "found outside, owner unknown". The outside places
  are the user's temp folder, the top level of each drive root, agenttalk's per-user
  folders and the old sibling scratch folder.
- **A failed scan is unknown**, never zero and never contained.
- **An unobserved location is never shown as contained**, and the report never
  claims universal containment.
- **File writes by programs are never "enforced" in stage 1.** With one Windows user
  for every team, any program can still write anywhere that user can. A folder
  convention is not a security boundary, and the report says so in its header.
- **Declared exceptions are listed** with their reasons, owners and review dates.

## Declared exceptions

These stay outside the team root on purpose:

| Exception | Reason |
|---|---|
| The AI tools' home folders (settings, login, session history) | Operator decision: login stays universal. Codex seats already get their own home inside the project's `.agenttalk/`. |
| Scheduled tasks (the supervisor's and the gateway's) | Windows keeps them per user, outside any folder. |
| The model gateway service and its per-user files (secrets, install record, spend ledger) | One gateway serves every team of the user; its folders are per user by design. |
| The Windows developer setting | A machine-wide registry setting, changed only by CI's own opt-in. |
| agenttalk's signing keys and backups | Keys are secrets kept per user; backups must survive the deletion of a team folder. |
| Programs installed for the whole machine (Python, the agenttalk runtime), including compiled files Python writes next to them | Programs, not work. Tools that a team's launcher takes from another project's folder are not an exception: they move into the team's `tools/`. |

**How a team adds one.** An entry in `team.exceptions` names what it is, where it
is, why, its owner and a review date. `doctor` lists it. An entry without a reason or
an owner is invalid, and strict mode reports it. Never allowed as an exception: the
bus, checkouts, scratch, temp files and caches for general work.

## The desktop migration step

On the maintainers' desktop host, the launcher (a local operations script outside
this repository) puts three tools from a sibling project's folder first on every
seat's path: a Maven build tool, a Java runtime and Node. It also points `JAVA_HOME`
and `MAVEN_HOME` there, and refuses to start if those folders are missing. Every Node
console test uses that Node. So the team's seats depend on another project's folder.

Stage 1 includes moving them, at a quiet restart:

1. Copy the three tools (about 0.4 GB) into `<team root>/tools/`.
2. Point the launcher's path entries, `JAVA_HOME` and `MAVEN_HOME` at the copies.
3. Run the launcher's `-EnvOnly` check, which prints the environment it would give
   the seats and starts nothing. Every tool location must be inside the team root.
   This is the proof that the move took.
4. Restart the seats, then run the Node console tests once.
5. Keep the old folder until that restart has worked. Pointing the launcher back is
   the rollback.

The same launcher now lets Codex seats write only inside the bus folder (their
"writable roots"). Once `tmp/`, `cache/` and `scratch/` exist, the migration step must
check what a Codex seat can still write there. If its sandbox allows only the bus,
add those folders to its writable roots. The canary ran no Codex seat, so this is
unverified.

This is the only move in stage 1. It moves programs, not work, so it needs no proof
that work was kept; the launcher check is enough. Moving the team's own folders is
separate (below).

## What stage 1 does not do

- **Move existing folders.** One team today is spread over several sibling folders.
  Moving them is a separate operation for a quiet window, with:
  - an inventory of every folder and worktree;
  - a rollback plan;
  - proof afterwards that no work was lost: unpushed commits, uncommitted files and
    each seat's ability to resume its session.
- **Give each team its own Windows user.** That is the strongest separation Windows
  offers (its own home, temp folder, logins and folder permissions), at the cost of
  a login per team. It is the operator's decision, for later.
- **Sandbox programs.** Nothing stops a program from writing elsewhere.
- **Prove anything on Linux or macOS.** The canary ran on Windows only; stage 1 turns
  it into tests that run on all three systems in CI.

## Kill signals

Stop and rethink if any of these happens:

- a move loses work or a seat's ability to resume;
- the clean-up removes anything whose safety it could not show;
- the clean-up follows a link outside its boundary;
- a restart loses the routing;
- the report shows a location as contained, or as observed, without the write, time
  and scope that observation needs.

## Stated limits

- **Same user, no boundary.** The team root is a convention that agenttalk applies
  and reports on. It does not stop anything from writing elsewhere.
- **A process that outlives its run.** Temp files are throwaway by contract once their
  run has ended. A program that keeps using a run's temp folder after the run ended
  can lose those files once they are older than `tmp_keep_days`. They stay restorable
  from the trash for `trash_keep_days`.
- **Unknowns remain.** These were not measured:
  - the real AI tools' temp and cache use (no paid turns ran);
  - git's, Node's and npm's internal temp use;
  - which tests write to the real home folders during a gate run;
  - everything on Linux and macOS.
- **Java ignores `TMPDIR` on Linux and macOS.** It uses `/tmp` there unless it is
  started with `-Djava.io.tmpdir`. On Windows it follows `TEMP`. Not measured.
- **Size scans have limits.** A very large folder can stop a scan early; the report
  then says the size is unknown.
- **Moving temp moves shared signals.** The Claude status line writes a small
  context file into `TEMP`, and the checkpoint hook reads it. Both run inside the seat,
  so they move together. A reader outside the seat would look in the wrong place.

## Open questions

1. Should `work/` get a size budget that only warns, or none?
2. Should backups move into the team root, with a copy kept per user?
3. What should the default numbers for the budgets and the keep days be on each host?
4. Should the safer clean-up rules also replace today's age-only rules for projects
   that are not in a team?

## Stage 1: the build, with acceptance cases

Four pull requests, in this order. Each functional one carries its own tests. The
sizes are estimates of changed lines.

1. **The setting and clean-up safety.** Files:
   - `team_folder.py` (new): reading and checking the setting, strict refusals,
     owner records, liveness, `CACHEDIR.TAG` checks;
   - `scratch.py`: the team's scratch root and the disposable marker;
   - `janitor.py`: the eligibility rules above, the recheck before every action,
     links at every action, the trash with its budget and permissions.

   About 700 to 900 lines of product code, and 900 to 1,100 lines of tests.
2. **Routing.** Files:
   - `supervisor.py`: run folders, owner records, the routed set applied last, the
     reserved names;
   - `cli.py`: `team run`;
   - the wrapper's start check and start probe;
   - the gateway-backed child's list;
   - the comprehension worker's `-B`;
   - the dev gate's run folder;
   - `wrapper_logs.py` and `turn_events.py`: their default folders;
   - the canary turned into tests on all three systems.

   About 500 to 700 lines of product code, and 700 to 900 lines of tests.
3. **The report, budgets and warnings.** Files: `doctor.py`, the console's data and
   the free-space checks. About 600 to 800 lines of product code, and 600 to 800
   lines of tests.
4. **Documentation.** README "Where agenttalk keeps files",
   `docs/ops/scratch-hygiene.md`, `docs/DEV-GATE.md`, CHANGELOG.

In total, about 1,800 to 2,400 lines of product code and 2,200 to 2,800 lines of
tests. That is roughly twice the first estimate, which left out the evidence rules
and the startup routing.

**Strict mode before the report exists.**
- After pull request 1, strict mode checks the setting: an invalid one makes
  `scratch root` and the clean-up refuse, and `doctor` shows one line, "team setting
  invalid: <reason>".
- After pull request 2, strict mode also refuses a wrapper start without routing and
  a seat or recovery environment that sets a routed name.
- Until pull request 3, nothing is shown as observed or contained; `doctor` says the
  containment report is not available yet.

**Acceptance cases.** Each must pass before its pull request merges.

- **Startup routing:**
  - a supervisor launch, a requested restart and a recovery relaunch each get a new
    run folder, with the routed set applied last;
  - a seat `env` that sets a routed name is refused in strict mode;
  - `team run` starts a wrapper by hand with routing;
  - a hand start without it is refused in strict mode, and only warned about
    otherwise;
  - the wrapper's start probe lands in its run folder;
  - Python's compiled-file folder in the wrapper equals the team's;
  - all of this on Windows, Linux and macOS in CI.
- **Live entries:**
  - a run folder whose process is still running is never eligible, however old;
  - a run folder whose liveness cannot be read is kept;
  - a file touched after the report is skipped at apply time;
  - an unmarked scratch folder is kept;
  - a marked one whose request is still open is kept;
  - a worktree with an ignored file that has no evidence is kept;
  - a `CACHEDIR.TAG` with the wrong signature gives no evidence;
  - a compiled file whose source is gone gives no evidence.
- **Links:** a link under `tmp/` pointing outside is removed as a link and its target
  is untouched. A worktree registered outside the team root is neither committed,
  pruned nor removed.
- **Trash pressure:**
  - a move to the trash is reported as "not freed";
  - `--purge-trash` and `--no-trash` need the owner's or the operator's permission;
  - a purge removes only trash entries;
  - the total budget counts the trash.
- **State retention:** `state/` over budget gives a warning and nothing else; the
  clean-up never touches it.
- **Scan failures:** an unreadable folder makes the size and the state "unknown", never
  zero or contained; a scan that stops early says so.
- **The comprehension worker:** it writes no compiled files, and its temp folder is
  the run folder.

## Appendix: canary evidence (Windows)

The canary is `tests/support/team_folder_canary/canary.py`. It uses no model, no
network and no gateway. It starts four children the way agenttalk starts them, once
with today's environment ("baseline") and once with the routed variables ("team"):

- **wrapper:** a wrapped seat's turn through the wrapper's real spawn path, using the
  stub model tool from the enforcement canary;
- **gateway:** a child with the gateway-backed environment, which only probes itself;
- **gate:** a pytest run with the dev gate's own export, environment and launcher;
- **comprehension:** a child with the comprehension worker's own environment and
  interpreter flags, which only probes itself.

Each probe records three kinds of fact:
- **configured:** the environment it saw, and the destinations Python and pip
  resolve for themselves. pip's cache folder is pip's own answer: no download and no
  cache write happened.
- **observed:** a temp file and a compiled file it actually wrote, and where they
  landed.
- **unknown:** anything else.

Locations are named by kind only, never by full local path. The raw outputs are
`tests/support/team_folder_canary/evidence/windows-py3.10.json` and
`windows-py3.14.json`. Both Python versions gave the same picture.

| Child | Mode | Temp folder (configured) | Temp file written (observed) | Compiled file written (observed) | Compiled file for installed code (configured) | pip cache (resolved) |
|---|---|---|---|---|---|---|
| wrapper process | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` |
| ordinary child | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` |
| gateway child | baseline | user temp | not written | next to the probe's code | next to the installed program | project folder |
| comprehension child | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` |
| dev gate | baseline | its run folder, in user temp | not run | not run | not run | not run |
| wrapper process | team | `tmp/` | `tmp/` | `cache/pycache/` | `cache/pycache/` | `cache/pip/` |
| ordinary child | team | `tmp/` | `tmp/` | `cache/pycache/` | `cache/pycache/` | `cache/pip/` |
| gateway child | team | `tmp/` | `tmp/` | next to the probe's code | next to the installed program | project folder |
| comprehension child | team | `tmp/` | `tmp/` | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` |
| dev gate child | team | its run folder in `tmp/` | run folder | run folder | next to the installed program | turned off |

The canary's probe code lives in the team root's `work/` folder, so "next to the
probe's code" lands inside the team root. For the code a real child imports, such as
the installed agenttalk, the "installed code" column applies. The canary predates the
run folders proposed above, so its `TEMP` is `tmp/` itself.

Baseline mode writes no temp file and does not run the gate, because both would
write into the user's real temp folder.

What the wrapper process resolved for agenttalk's own folders with the routed
variables set (configured):

- **inside `state/`:** the turn journal;
- **inside `tmp/`:** the clean-up command's temp folder and the status line's context
  file;
- **still in the user's `AppData/Local/agenttalk`:** the wrapper logs, the signing
  keys and backups;
- **in `AppData/Local/agenttalk-ovh`:** the gateway's secrets;
- **the scratch root:** still the default sibling folder next to the project, because
  it follows the scratch setting, not `AGENTTALK_SCRATCH`.

About the user's temp folder, the canary claims only what one listing at the start
and one at the end can show: the new top-level names still there at the end. It
cannot see a file that was made and removed during the run, a write inside a folder
that already existed, or an overwrite. It says nothing about who made a new name.

In the recorded runs, both listings succeeded. On Python 3.14 no new top-level name
remained. On Python 3.10 one remained, and its name was not one the canary uses;
whose it is, is unknown.

Not verified: Linux and macOS, the real AI tools, git, Node and npm.

## Technical details

- **Existing code this design builds on:**
  - `src/agenttalk/scratch.py`: `resolve_scratch_root` falls back silently to
    `default_scratch_root`.
  - `src/agenttalk/janitor.py`:
    - `JanitorConfig.tmp_root` defaults to the system temp folder;
    - `find_candidates` ages temp and scratch entries by their newest file;
    - `is_dirty_worktree` uses `git status --porcelain`, which hides ignored files;
    - `wip_commit_dirty_worktree` and the prune step are the mutations that need the
      link check.
  - `src/agenttalk/supervisor.py`:
    - the generated `Launch` and `Launch-Spec` apply `AGENTTALK_ROOT`,
      `AGENTTALK_PY`, `AGENTTALK_SCRATCH` and `CODEX_HOME` first, then `$a.env` or
      `$spec.env`;
    - gateway-backed seats refuse `$a.env`;
    - `New-WrapperLogTargets` keeps `$WrapperLogGenerations` generations.
  - `src/agenttalk/wrapper/run.py`: `_child_env` is an allowlist for the
    gateway-backed backend and passes everything for the others.
  - `src/agenttalk/dev_gate.py`: `_base_env`, `_default_external_base` and
    `execute_gate`; run-folder retention is #338.
  - `src/agenttalk/comprehension/worker.py`: `_ALLOWED_ENV_VARS`,
    `sanitized_worker_env` and `_worker_subprocess_argv` (`-s -S`).
  - `src/agenttalk/ovh_gateway_service.py`: `_base_gateway_environment`.
  - `src/agenttalk/wrapper_logs.py`: `default_wrapper_log_root` (the ring is bounded
    by `AGENTTALK_WRAPPER_LOG_MAX_BYTES`).
  - `src/agenttalk/turn_events.py`:
    - `default_turn_events_root`, with `AGENTTALK_TURN_EVENTS_DIR`;
    - the per-seat cap `TURN_EVENTS_MAX_BYTES`, enforced by `_enforce_cap`;
    - `_prune_old_status`.
  - `src/agenttalk/capacity.py` (`read_claude_context_sidecar`) and
    `src/agenttalk/checkpoint.py` (`collect_context`).
  - Process identity for liveness: the start-token helpers agenttalk already has
    (`supervisor._start_tokens_match`, `supervisor_lifecycle.start_tokens_match`).
- **Canary:**
  - `tests/support/team_folder_canary/canary.py`: the launcher, the four inner runs
    and the user-temp summary;
  - `probe.py`: standard library only; pip's answer is taken only from a successful
    run's standard output, as one absolute path;
  - `child.py`: it probes, then runs `tests/support/stub_cli.py`.
  - `tests/test_team_folder_canary.py` pins the two evidence rules:
    - a warning on pip's standard error never becomes the answer;
    - a failed listing is unknown, not empty.

  Run it with `python tests/support/team_folder_canary/canary.py --team-root <new
  folder> --out <file>`, with `PYTHONPATH=src`, `AGENTTALK_ROOT` unset and no
  gateway variables set.
- **Issue and challenge:** #336; challenge `ch-ac2ae695-441c-46b2-8605-747c3b040474`
  (reshape, accepted). Dev-gate run folders: #338.
