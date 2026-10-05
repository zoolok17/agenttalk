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
with a named owner, all through the existing clean-up command. The clean-up removes a
folder only when it can show that nothing still uses it, and keeps everything else. A
**containment report** says, for each location, whether it is only configured,
actually seen, unknown or enforced, and it never claims more than it saw.

This is not a security boundary. Every team still runs as the same Windows user, so
any program a seat starts can still write anywhere that user can. Moving the folders
of teams that already exist is a separate, later operation. Stage 1 moves only one
small thing: the build tools that one host's seats borrow from another project's
folder go into the team's own folder.

What this document decides: the team root's layout and settings, which variables each
kind of child process gets and how they are checked before a process starts, which
files the clean-up may remove and on what evidence, how the trash keeps things
restorable, how limits and warnings work and who owns them, the four states of the
report, the declared exceptions, and what stage 1 builds, with acceptance cases. What
it leaves open is listed under "Open questions".

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
| `scratch/<seat>/<task>/` | Each seat's throwaway work (`AGENTTALK_SCRATCH`). | Only folders their seat has released, once everything that could still use them has ended. Everything else is kept and reported. |
| `tmp/<seat>/<run>/` | One folder per wrapper run: that run's temporary files (`TEMP`, `TMP`, `TMPDIR`) and those of everything it starts. | Only folders of runs whose every process has provably ended. |
| `cache/` | Download and build caches: pip, npm, compiled Python files, Maven. | Only the tool caches listed below, and only by size and age. |
| `state/` | agenttalk's own per-team records: wrapper logs, the turn journal. | Nothing. Each writer limits its own size. |
| `trash/` | What the clean-up removed, with a record of where each item came from, so it can be restored. | Deleted after a set age, or early with permission. Entries of unknown origin are kept. |
| `team.json` | The team's settings (below). | Nothing. |

"Inside the team root" never means "safe to delete". The clean-up acts only on the
folders the table names, only on the evidence described under "What the clean-up may
remove", and keeps anything whose safety it cannot show.

### Settings

The team's settings live once, in `<team root>/team.json`. Each of the team's projects
only points at it from its `.agenttalk/config.json`:

```json
{"team": {"root": "D:/Teams/example"}}
```

and `team.json` holds everything else:

```json
{
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
  "retention": {"tmp_keep_days": 2, "scratch_keep_days": 7, "trash_keep_days": 7, "cache_keep_days": 14},
  "report": {"observation_max_age_minutes": 1440},
  "tools": {"java": "tools/java", "maven": "tools/maven", "node": "tools/node"},
  "owner": "example-lead",
  "exceptions": []
}
```

(Partial example of the proposed format, not runnable yet. The numbers are
placeholders for the operator to set, not recommendations.)

- **One copy for the whole team.** Every project of the team reads the same file, so
  budgets, the owner and the exceptions cannot differ between projects.
- **`strict`** is the switch the operator turns on per team. With it on, a setting
  that does not hold refuses instead of falling back:
  - a pointer to a root with no `team.json`, or a `team.json` whose `root` is not its
    own folder;
  - a root that is missing, relative, unwritable or not a folder;
  - a subfolder that a link or junction takes outside the root;
  - a `scratch.root` outside the root;
  - an exception without a reason.

  `agenttalk scratch root` and the clean-up exit with an error, and the supervisor
  does not start the team's seats. Today an unusable scratch setting silently falls
  back to the shared sibling `atk-scratch` folder; strict mode never does.
- **Without the `team` pointer**, or with `strict` off, everything behaves as it does
  today, including today's clean-up rules (see "Open questions").
- The existing `scratch` settings stay. In a team, the scratch root defaults to
  `<team root>/scratch`, and `scratch.root` must lie inside the team root.

## Where each kind of child gets its locations

A seat is a chain of processes, and the variables travel down the chain. The
challenge's point stands: changing the launcher alone is not enough, because several
links in the chain build their own environment.

### The routed destinations

All inside the team root:

| Destination | Set by | Points to | Who reads it |
|---|---|---|---|
| Temp files | `TEMP`, `TMP` | `tmp/<seat>/<run>/` | Windows programs, Python, Node |
| Temp files | `TMPDIR` | `tmp/<seat>/<run>/` | Python everywhere; many programs on Linux and macOS |
| Java's temp files (Linux and macOS) | `JDK_JAVA_OPTIONS`, holding only `-Djava.io.tmpdir=<run folder>`, computed by agenttalk | `tmp/<seat>/<run>/` | Java, which ignores `TMPDIR` there (on Windows it follows `TEMP`) |
| pip's cache | `PIP_CACHE_DIR` | `cache/pip/` | pip |
| npm's cache | `npm_config_cache`, and `cache=` in the team Node's own global `npmrc` | `cache/npm/` | npm |
| Compiled Python files | `PYTHONPYCACHEPREFIX` | `cache/pycache/` | Python |
| Other tool caches | `XDG_CACHE_HOME` | `cache/xdg/` | Linux and macOS tools that follow the XDG rules |
| Maven's downloads | `localRepository` in the team Maven's own `conf/settings.xml` (no variable) | `cache/maven/` | that Maven, from any process that runs it |
| Scratch | `AGENTTALK_SCRATCH` | `scratch/<seat>/` | seats (already set today from the scratch setting) |
| The turn journal | `AGENTTALK_TURN_EVENTS_DIR` | `state/turn-events/` | the journal's writer |
| Wrapper logs | the supervisor's log targets | `state/wrapper-logs/` | the supervisor and the wrapper |
| The tools | `JAVA_HOME`, `MAVEN_HOME` and the tools' `bin` folders first on `PATH` | `tools/` | Java and Maven builds, Node tests (set by the host's launcher; see "The desktop migration step") |

Configuring Maven and npm inside the team's own copies of those tools, not only
through variables, means the setting travels with the program into every child
boundary, including the ones that drop variables.

What stage 1 does **not** move: `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA`.
Moving them would also move the AI tools' logins, agenttalk's signing keys and the
gateway's secrets, which the operator decided stay per user (see "Declared
exceptions").

### One check for every reserved destination

One check, used in three places, validates every destination in the table above:
- the supervisor before it starts a seat;
- `agenttalk team run` before it starts a command;
- the wrapper as its first step.

The check:
- **Temp:** `TEMP`, `TMP` and `TMPDIR` must all name the same run folder of this
  team, and that folder's owner record must name this seat.
- **Caches:** `PIP_CACHE_DIR`, `npm_config_cache`, `PYTHONPYCACHEPREFIX` and
  `XDG_CACHE_HOME` must each name its own folder under `cache/`.
- **agenttalk's own folders:** `AGENTTALK_SCRATCH` must name `scratch/<seat>/`, and
  `AGENTTALK_TURN_EVENTS_DIR` and the wrapper-log targets must lie under `state/`.
- **Tools:** for each tool `team.json` declares, `JAVA_HOME` and `MAVEN_HOME` must lie
  under `tools/`, and that tool's `bin` folder must come before any other copy of it
  on `PATH`.
- **Java on Linux and macOS:** `JDK_JAVA_OPTIONS` must hold exactly the computed
  value.
- **In the wrapper only:** Python's compiled-file folder must equal
  `PYTHONPYCACHEPREFIX`.

A missing or wrong value is reported by its name. In strict mode it refuses the
start; with strict off it records a warning.

### 1. Supervisor to seat: routing is set before the process starts

Python fixes its compiled-file folder when it starts, and its temp module remembers
the temp folder it chose the first time it is asked. Changing the variables inside a
process that is already running is therefore too late; the reviewer measured this on
both Python versions. So the variables must already be in the environment when the
wrapper's interpreter starts.

- **Supervisor launches and restarts.** For each launch, the supervisor creates a
  fresh run folder `tmp/<seat>/<run>/`, writes an owner record into it (the seat and
  the run) and applies the routed variables. Restarts, including a requested restart
  and a recovery relaunch, go through the same launch path, so each gets its own run
  folder with the same routing.
- **Precedence.** Today the supervisor applies its own entries first, then the seat's
  own `env` setting (or, for a recovery launch, the recovery environment). Stage 1
  applies the routed set **last**, and the routed names are reserved: in strict mode
  a seat's `env` or a recovery environment that sets any of them is refused, at
  config validation and at launch. A gateway-backed seat already refuses a literal
  `env` setting; the routed set is computed, not literal, so that refusal stays as it
  is.
- **Hand starts.** A new command, `agenttalk team run --for <seat> -- <command>`,
  creates the run folder, sets the routed variables, runs the full check and starts
  the command. It is the supported way to start a seat by hand.
- **The wrapper checks itself first.** Before it imports anything that uses temp
  files, the wrapper runs the full check on its own environment.
  - If the check passes, the wrapper joins its run's job (next section), and then
    adds its process id and process start token to the owner record.
  - If the check fails, the wrapper refuses to start in strict mode, naming the
    supported command; with strict off it records a warning.
  - The wrapper does not restart itself with a corrected environment: on Windows that
    would give it a new process id, and the supervisor tracks the one it started.
- **What can be written before the check.** When the routing is missing (a hand start
  without `team run`), starting Python and importing the wrapper's entry modules can
  write compiled files next to the installed agenttalk runtime. Those files belong to
  the installed-program exception. Usually there are none, because pip compiles them
  at install. No temp file is created before the check: it reads only environment
  variables and the owner record.

### 2. Wrapper to an ordinary model child

The wrapper passes its whole environment to the model's command-line tool, minus a
few of its own markers. Whatever the wrapper has, the tool and everything the tool
starts inherit.

Canary (Windows):
- With the routed variables set, the child's temp folder, a file it wrote and a
  compiled file it wrote were inside the team root, and pip's resolved cache folder
  was too.
- `JAVA_HOME` and `MAVEN_HOME` arrived unchanged.
- Without the routed variables, temp and pip's cache resolved to the user's temp
  folder and the user's home.

Not observed: the real AI tools' own use of temp and caches, because no paid turn
ran. Until a seat's own observation exists, the report says "unknown".

### 3. Wrapper to a gateway-backed child

This child gets a deliberately small environment, because a paid outside worker must
not learn the operator's real home:
- it keeps `PATH`, the Windows system folders, `TEMP`, `TMP`, two Python text settings
  and every `AGENTTALK_` variable, and drops everything else;
- it sets `HOME` and `USERPROFILE` to the project folder, and `LOCALAPPDATA` and
  `APPDATA` to folders below it.

Canary (Windows):
- Its temp folder followed the team's `TEMP`, and so did a file it wrote.
- pip's cache folder resolved into the project folder: inside the team root, but
  mixed in with the bus and the checkout.
- A compiled file for installed code would go next to the installed program, because
  the compiled-file setting is dropped.
- `JAVA_HOME` and `MAVEN_HOME` were dropped.

`TMPDIR` does not reach this child either. That alone does not decide where it puts
temporary files:
- Python checks `TEMP` and `TMP` too, and the child keeps both; the canary measured
  this for Python on Windows.
- Node's runtime library documents that it checks `TMP` and `TEMP` after `TMPDIR`;
  this was not measured.
- A program that reads only `TMPDIR` would fall back to its own default. Whether this
  child runs such a program, and anything on Linux and macOS, is unmeasured.

Stage 1, at this boundary:
- **Paths that pass, each only when it lies inside the team root:** `TMPDIR`,
  `PIP_CACHE_DIR`, `npm_config_cache`, `PYTHONPYCACHEPREFIX` and `XDG_CACHE_HOME`,
  and, for the tools `team.json` declares, `JAVA_HOME` and `MAVEN_HOME` when they lie
  under `tools/`.
- **`JDK_JAVA_OPTIONS`** is computed again from the child's own `TEMP`, never passed
  on.
- **Maven's and npm's caches** follow the team's own copies of those tools through
  their settings files.

No option string is ever passed through this boundary as it is. These are folders
inside the same team root as `TEMP`, which the child already gets.

### 4. The dev gate and its tools

The dev gate builds its own environment from a short list:
- it sets `TEMP`, `TMP` and `TMPDIR` to its own run folder;
- it turns pip's cache off;
- it passes on `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA`;
- it drops `JAVA_HOME`, `MAVEN_HOME`, `MAVEN_OPTS` and the other tool settings;
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
- `JAVA_HOME` and `MAVEN_HOME` were dropped.
- Tests still see the user's real home folders. Which tests write there is unknown.

Stage 1, at this boundary:
- When a team root is set, the gate puts its run folder under the seat's run folder
  itself, not only through `TEMP`, so a gate run started from an ordinary shell lands
  there too. Removing old gate run folders is #338.
- **The tool settings are deliberately not passed to the gate.** The gate runs
  agenttalk's own Python checks, and no Java, Maven or Node.
  - A gate test that started Maven anyway would get the team Maven first on `PATH`,
    with its own settings file, but no `JAVA_HOME` or `MAVEN_HOME`. That case is
    unmeasured, and the report lists it as an exclusion.
  - No option string is passed on.

### 5. The comprehension worker

The worker gets a fixed short list:
- it keeps `PATH`, the Windows system folders, the home variables, `TEMP`, `TMP` and
  `TMPDIR`, and sets `PYTHONPATH` to the agenttalk it runs;
- it drops `PYTHONPYCACHEPREFIX`, `PYTHONDONTWRITEBYTECODE`, the cache variables and the
  tool settings;
- it starts Python with `-s -S`, which skip the user and site folders but do not stop
  compiled files from being written.

Canary (Windows):
- Its temp folder and a file it wrote followed the team's `TEMP`.
- A compiled file for installed code, such as the agenttalk it imports, would go next
  to the installed program, outside the team root.
- pip's cache folder resolved to the user's home. The worker runs no pip.
- The tool settings were dropped.

Stage 1 policy:
- **No compiled files:** start the worker with `-B` as well, so it writes none. No new
  path crosses the worker's privacy list. The worker then compiles in memory each time
  it starts, which costs a little time per scan.
- **Tools:** the tool settings stay out, because the worker parses files and runs no
  tools. The report lists this as an exclusion.

### 6. Other children agenttalk starts

- **Packaging checks:** they use Python's temporary folders, which follow the seat.
- **The gateway service:** a declared exception (below). It runs from its own
  scheduled task, with that task's temp folder.

## What the clean-up may remove

The clean-up removes a folder only when it can show that nothing still uses it. When
it cannot show that, it keeps the folder and reports it. Neither age, nor a folder's
name, nor the end of one process shows that.

### Proving that every process of a run has ended

The process a run started first, the wrapper, can end while a program it started is
still running and still needs a file in the run's temp folder, even one that has not
changed for days. So the proof must cover every process of the run, not only the
first.

- **Windows: one job per run.** The wrapper's first step after its routing check
  joins a Windows job object created for its run and named after the run id. Every
  process the run then starts, and everything those start, is in the same job.
  - The job allows no breakaway, so no process of the run can leave it.
  - It does not kill anything when it closes, so nothing behaves differently for a
    seat's programs.

  agenttalk already uses job objects to contain process trees; this one is a variant
  that does not kill on close.
- **Reading the proof.** The supervisor, which is outside the job, checks each run
  whose wrapper has ended:
  - The run counts as ended only when the job reports no active processes on two
    checks some minutes apart, both after the wrapper's own process was seen to have
    ended. One check is not enough: a job's count can drop a moment before the
    process is really gone.
  - If the job still has an active process, the run is still running.
  - If the supervisor cannot read the job's state, the run's state is unknown. That
    covers a failed join, a job that cannot be opened, and a supervisor that
    restarted and cannot open the job by its recorded name with a certain result.
- **Linux and macOS:** stage 1 has no trusted proof there, so no run folder is ever
  removed automatically. Budgets still warn.

### Temp run folders: `tmp/<seat>/<run>/`

A run folder may go to the trash only when all of these hold:

1. **Its owner record is readable**, and names this team's seat and run.
2. **Every process of the run has provably ended**, as above. A run that is still
   running, or whose state is unknown, keeps its folder.
3. **Nothing in it was touched recently.** The newest file anywhere inside is older
   than `tmp_keep_days`, measured over the whole tree. This is a margin on top of
   point 2, never a replacement for it.
4. **No link takes it outside the team root** (see "Links").

Anything directly in `tmp/` that is not a run folder has no known owner. It is kept
and reported.

### Scratch task folders: `scratch/<seat>/<task>/`

Scratch belongs to one seat: anything another seat needs goes in `work/`. Scratch
folders are kept unless their seat releases them. When a seat has finished with a
task folder, it says so with `agenttalk scratch release --for <seat> --task <task>`.
That writes a release record naming the seat, the task and the run that released it.

A released folder may go to the trash only when all of these hold:

1. **Every process of the releasing run has provably ended.** That covers anything the
   seat started that might still use the folder.
2. **No later run of any seat has used it.** Any write after the release cancels the
   release.
3. **The newest file anywhere inside is older than `scratch_keep_days`.**
4. **No link takes it outside the team root.**
5. **It holds no worktree that the worktree rules below would keep.**

A closed request is never proof on its own; the report may show it as a hint, "request
closed, not released". An unreleased folder is never removed automatically. When
scratch is over its budget, the owner gets an attention item listing the largest
unreleased folders, for the owning seat or a person to decide. Today's clean-up
removes any scratch task folder older than its keep days; in a team, these rules
replace that.

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
value". A worktree also counts as a folder of its run or task: it is never removed
while that run's or task's proof is missing.

### Caches: `cache/`

Only caches that their tools define as caches are trimmed, and only when `cache/` is
over its budget:
- pip's and npm's caches, which both tools document as caches they refill on demand;
- the compiled Python files, which Python rebuilds from source;
- folders under `cache/xdg/` with a valid `CACHEDIR.TAG`.

Even then, only entries whose newest file is older than `cache_keep_days` are
trimmed, oldest first. A file a running tool holds open is skipped. Cache entries are
deleted directly, not moved to the trash: a cache holds only copies that can be
fetched or rebuilt, never work.

Maven's local repository is not trimmed. Besides downloads, it holds artifacts built
locally with `mvn install`, which may have no other copy. Over budget, it gives a
warning only.

### Everything else today's clean-up removes

In a team, the clean-up keeps and reports everything it removes today by name and
age alone:
- the entries in a checkout's root that match its name patterns;
- every folder in a checkout's `.worktrees/` folder;
- the entries in the shared system temp folder that match its temp patterns.

A worktree anywhere in the team root is removed only as part of a run or scratch
folder that passes its own proof above. Outside the team root, the clean-up only
reports.

### Every action is checked again just before it happens

The report finds candidates. Applying it repeats every check above for each entry
immediately before acting on it:
- the owner and the run's proof;
- the release and any later use;
- the newest-file age;
- the git state;
- links and the real location.

If anything changed since the report, that entry is skipped and reported as "changed
since the report".

### Links

- **Walking.** The clean-up never follows a link or junction while walking a folder.
- **Before every change.** Before a WIP commit, a worktree prune, a move to the trash
  or a deletion, it resolves the entry's real location, and refuses and reports if
  that lies outside the team root.
- **A candidate that is itself a link,** found directly in `tmp/`, `scratch/<seat>/`
  or `cache/`, is refused and reported, whatever it points at.
- **A link nested inside an otherwise eligible folder** is removed as a link, by a
  delete that does not follow it; its target is never touched.
- **Prunes.** It prunes a worktree registration only when the registration's recorded
  path lies inside the team root.
- **Stubborn folders on Windows.** Today's clean-up has a fallback for folders a
  plain delete cannot remove:
  - it mirrors an empty folder over them with `robocopy /MIR`, without `/XJ`;
  - it takes ownership with `takeown /R` and resets permissions with `icacls /T`.

  All three can reach through a junction nested inside the folder and change or
  delete what it points at. In a team, the fallback first removes every nested link
  as a link without following it, runs `robocopy` only with `/XJ`, and refuses if a
  nested link cannot be removed. The same pattern exists in today's clean-up outside
  teams; fixing it there is a separate change.

## The trash

Moving something to the trash must keep it restorable. Keeping its bytes alone is not
enough: the clean-up must also know where each item came from and why it was removed.

- **Layout.** Each removal gets its own slot, `trash/<time>-<id>/`, holding two things:
  - `payload/`: the moved file or folder;
  - `origin.json`: where it came from (its path relative to the team root), what kind
    of entry it was, the evidence that made it eligible, its size, when it was
    moved, and a state.
- **Moving.** The clean-up writes `origin.json` with the state "moving" before it
  moves anything. It then renames the entry into `payload/`, a rename on the same
  drive, and finally sets the state to "moved".
- **Interrupted moves.** At its next start, the clean-up checks every slot still in
  "moving":
  - **The entry is still at its origin and `payload/` is empty:** the move never
    happened, and the slot is removed.
  - **The entry is in `payload/` and nothing is at its origin:** the move completed,
    and the state becomes "moved".
  - **Anything else** (both, neither, or an unreadable record): the slot is kept and
    reported. It is never purged.
- **Restoring.** An item can be restored by moving `payload/` back to its origin.
  Restoring refuses, and reports, when:
  - something already exists at the origin;
  - a folder on the way to the origin is a link, or lies outside the team root;
  - the slot's state is not "moved".

  Links inside the payload are moved back as links, never followed. The procedure is
  specified in stage 1; a restore command is optional.
- **Purging.** A slot is deleted after `trash_keep_days`, or earlier with permission
  (below), only when its `origin.json` is readable, its state is "moved" and its
  payload is intact. Anything else in `trash/` is of unknown origin: a slot with a
  damaged record, or a file someone put there by hand. It is kept and reported, never
  purged.

## Limits, warnings and freeing space

Putting files together does not stop a disk from filling. Stage 1 adds:

- **Free-space warnings.** `doctor`, the console and the supervisor check the free
  space on each drive that matters:
  - the drive holding the team root;
  - the drive holding the user's home;
  - the drive holding the user's temp folder.

  Each drive is found from the actual folder, wherever it is redirected, and each
  is checked once.
  - Below `warn_free_percent` on any of them, it is an attention item for the team's
    owner.
  - Below `stop_free_percent`, the dev gate refuses to start, because it is the
    largest writer, and the supervisor raises an urgent item.
  - Running seats are never stopped for this: stopping them would lose work.
- **Budgets.** Each folder in the settings has a limit, and `total` covers the team
  root as a whole, the trash included.
  - The clean-up report measures them with a bounded scan.
  - A scan that hits its time or entry limit, or cannot read a folder, says "size
    unknown", never a guess and never zero.
  - Over budget is an attention item.
- **The trash frees no space.** Moving files to `trash/` on the same drive does not
  give space back. So:
  - Every clean-up result says two numbers separately: space freed, and space moved
    to the trash and not freed.
  - When space is low and the trash holds files, it says so plainly, for example: "12
    GB moved to the trash; no space was freed; `--purge-trash` would free 12 GB".
  - Freeing that space early needs permission from the team's owner or the operator:
    - `--purge-trash` deletes purgeable slots, oldest first;
    - `--no-trash` deletes newly eligible entries without the trash step.
  - Both only shorten the restore window for entries that already passed every check
    above. Neither touches an entry that failed one, or a trash entry of unknown
    origin.
  - Without that permission, nothing more happens, and the result says no space was
    freed.
- **The `state/` folder** has a warning-only budget. Its writers already limit
  themselves:
  - the supervisor keeps a set number of wrapper-log generations per seat, and each
    log is a bounded ring;
  - the turn journal's writer keeps each seat's journal under 64 MiB, removing its
    oldest files first, and keeps the newest four older status files;
  - the journal's list of runs (`streams.jsonl`) grows by one short line per run.

  The clean-up never touches `state/`, and stage 1 adds no new state retention.

**Who owns it.** The operator sets the policy in `team.json`. The team's `owner`,
normally its lead seat, runs the clean-up report on its routine (daily), applies it
when a budget is exceeded or at a quiet time, and is the one who can give the trash
permissions. `doctor` and the console show the state to everyone. There is one
cleaner, the existing `agenttalk janitor`; stage 1 adds no second one.

## The containment report

`agenttalk doctor` and the console gain a containment section. Each location gets
exactly one of four states:

| State | Meaning | Example |
|---|---|---|
| configured | agenttalk's settings or records say this location points inside the team root. No write there has been seen. | A seat's `PIP_CACHE_DIR` in its launch settings; the paths a wrapper resolved at start. |
| observed | A specific write or file was seen at a stated time, within a stated scope. | The wrapper's start probe wrote its file inside its run folder at 09:14; a scan of `tmp/` completed at 09:20 found 3,210 files. |
| unknown | Nothing tells us, the evidence is too old or its time cannot be trusted, or a scan failed. | The AI tool's own caches; tests writing to the real home folders during a gate run; anything on Linux or macOS until measured. |
| enforced | agenttalk itself refuses the alternative. | Strict mode refuses to start a wrapper whose routing check fails; the clean-up refuses to act outside the team root. |

Rules:

- **Configuration is never an observation.** Resolved paths, settings and start
  records are "configured", however exact they are.
- **An observation names its write, time and scope.** Two sources:
  - the wrapper's start probe: right after its routing check, the wrapper creates and
    removes one small file through Python's temp module, and records where it landed
    and when;
  - the clean-up report's scans, each recording when it ran, which folders it
    covered, and whether it finished.
- **Observations expire.** `report.observation_max_age_minutes` in `team.json` sets
  how long one counts (default 1440, one day). An observation becomes "unknown, last
  observed at <time>" when:
  - it is older than that;
  - its time cannot be read;
  - its time lies more than five minutes in the future.

  A clock set back makes old observations look future, and a clock set forward makes
  them look old. Either way they become unknown, never newer than they are.
- **Found is not attributed.** A scan of outside places reports what it found. It
  attributes an entry to this team only when the entry carries agenttalk's own naming
  for this team: for example a wrapper-log folder named by this project's id, or a
  run folder with this team's run id.
  - Anything else, including same-user names such as `pytest-of-<user>`, is "found
    outside, owner unknown".
  - The outside places are the user's temp folder, the top level of each drive root,
    agenttalk's per-user folders and the old sibling scratch folder.
- **A failed scan is unknown**, never zero and never contained.
- **An unobserved location is never shown as contained**, and the report never
  claims universal containment.
- **Exclusions are listed.** Child boundaries that deliberately do not get a setting
  (for example the tool settings in the dev gate and the comprehension worker) are
  listed as exclusions, not as contained.
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

1. **Copy without following links.** Copy the three tools (about 0.4 GB) into
   `<team root>/tools/`. First check that the source folders hold no links or
   junctions, and refuse if they do. Use a copy that does not follow links (for
   `robocopy`, with `/XJ`).
2. **Check the copy is complete.** Compare a list of every file, its size and its
   SHA-256 between the original and the copy. Any difference stops the migration.
3. **Configure the copies.**
   - Point the launcher's path entries, `JAVA_HOME` and `MAVEN_HOME` at the copies.
   - Set `localRepository` to `cache/maven/` in the team Maven's `conf/settings.xml`.
   - Set `cache=` to `cache/npm/` in the team Node's global `npmrc`.
   - Check that the user's own Maven and npm settings files do not override either.
4. **Check the configuration.** Run the launcher's `-EnvOnly` check, which prints the
   environment it would give the seats and starts nothing. Every tool location must
   be inside the team root. This proves the paths and settings only, not that the
   tools work.
5. **Prove each tool works.** Run short offline smoke commands under the environment
   `-EnvOnly` printed, each with a time limit and none with network access (npm's
   update check turned off):
   - `java -version` and `javac -version`;
   - `mvn -v`, whose output must name the copied Maven and the copied Java;
   - `node --version`;
   - `npm --version`, and `npm config get cache`, which must print the team's npm
     cache.

   Every command must succeed.
6. **Restart and test.** Restart the seats, then run the Node console tests once.
7. **Keep the originals until every check above has passed.** Pointing the launcher
   back is the rollback.

The same launcher now lets Codex seats write only inside the bus folder (their
"writable roots"). Once `tmp/`, `cache/` and `scratch/` exist, the migration step must
check what a Codex seat can still write there. If its sandbox allows only the bus,
add those folders to its writable roots. The canary ran no Codex seat, so this is
unverified.

This is the only move in stage 1. It moves programs, not work, and the checks above
prove the copies are complete and working. Moving the team's own folders is separate
(below).

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
- **Remove run or scratch folders automatically on Linux or macOS.** There is no
  trusted proof there yet that every process of a run has ended. Budgets warn, and
  the folders are kept.
- **Prove anything on Linux or macOS.** The canary ran on Windows only; stage 1 turns
  it into tests that run on all three systems in CI.

## Kill signals

Stop and rethink if any of these happens:

- a move loses work or a seat's ability to resume;
- the clean-up removes anything whose safety it could not show;
- the clean-up follows a link outside its boundary;
- a restore overwrites something or goes through a link;
- a restart loses the routing;
- the report shows a location as contained, or as observed, without the write, time
  and scope that observation needs.

## Stated limits

- **Same user, no boundary.** The team root is a convention that agenttalk applies
  and reports on. It does not stop anything from writing elsewhere.
- **Unknowns remain.** These were not measured:
  - the real AI tools' temp and cache use (no paid turns ran);
  - git's, Node's and npm's internal temp use;
  - which tests write to the real home folders during a gate run;
  - everything on Linux and macOS, including Java's `JDK_JAVA_OPTIONS` route, which
    also makes Java print a notice on its error output that a stage-1 test must
    check does no harm.
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
   - `team_folder.py` (new): `team.json`, the pointers, strict refusals, the full
     routing check, owner records, reading a run's job, `CACHEDIR.TAG` checks;
   - `scratch.py`: the team's scratch root and `scratch release`;
   - `janitor.py`: the eligibility rules, the recheck before every action, links at
     every action, the safe Windows fallback, the trash slots with restore and purge
     rules, and the trash permissions.

   About 900 to 1,200 lines of product code, and 1,100 to 1,400 lines of tests.
2. **Routing.** Files:
   - `supervisor.py`: run folders, owner records, the routed set applied last, the
     reserved names, the routing check before launch, reading run jobs;
   - `cli.py`: `team run`;
   - the wrapper's start check, joining the run's job, and its start probe;
   - the gateway-backed child's list;
   - the comprehension worker's `-B`;
   - the dev gate's run folder;
   - `wrapper_logs.py` and `turn_events.py`: their default folders;
   - the canary turned into tests on all three systems.

   About 650 to 900 lines of product code, and 850 to 1,100 lines of tests.
3. **The report, budgets and warnings.** Files:
   - `doctor.py` and the console's data;
   - the free-space checks;
   - the observation lifetime.

   About 600 to 800 lines of product code, and 600 to 800 lines of tests.
4. **Documentation.** README "Where agenttalk keeps files",
   `docs/ops/scratch-hygiene.md`, `docs/DEV-GATE.md`, CHANGELOG.

In total, about 2,150 to 2,900 lines of product code and 2,550 to 3,300 lines of
tests. That is roughly two and a half times the first estimate, which left out the
evidence rules, startup routing, per-run jobs and the trash contract.

**Strict mode before the report exists.**
- After pull request 1, strict mode checks `team.json` and the pointers: an invalid
  setting makes `scratch root` and the clean-up refuse, and `doctor` shows one line,
  "team setting invalid: <reason>".
- After pull request 2, strict mode also refuses any start whose routing check fails,
  and any seat or recovery environment that sets a routed name.
- Until pull request 3, nothing is shown as observed or contained; `doctor` says the
  containment report is not available yet.

**Acceptance cases.** Each must pass before its pull request merges.

- **Settings:**
  - two projects pointing at one `team.json` read the same budgets and owner;
  - a pointer to a root without `team.json`, or a `team.json` whose root is
    elsewhere, is refused in strict mode.
- **Startup routing:**
  - a supervisor launch, a requested restart and a recovery relaunch each get a new
    run folder, with the routed set applied last;
  - for every reserved destination in turn, one missing value and one wrong value are
    each refused, both at launch and at wrapper start, and the refusal names that
    variable;
  - a seat `env` that sets a routed name is refused in strict mode;
  - `team run` starts a wrapper by hand with routing, and a hand start without it is
    refused in strict mode, and only warned about otherwise;
  - the wrapper's start probe lands in its run folder;
  - Python's compiled-file folder in the wrapper equals the team's;
  - all of this on Windows, Linux and macOS in CI.
- **Every process of a run (Windows):**
  - the wrapper ended but a child it started is alive: the folder is kept;
  - a child that tries to break away from the job stays in it;
  - the job cannot be joined or read: the folder is kept;
  - after a supervisor restart the job's state cannot be established: the folder is
    kept;
  - one empty reading alone does not make the run ended; two readings some minutes
    apart, after the wrapper's process ended, do;
  - on Linux and macOS no run folder is ever removed automatically.
- **Scratch:**
  - an unreleased folder is kept;
  - a released folder is kept while any process of the releasing run is alive, or its
    state is unknown;
  - a write after the release cancels the release;
  - a closed request alone never makes a folder eligible.
- **Worktrees and caches:**
  - a worktree with an ignored file that has no evidence is kept;
  - a `CACHEDIR.TAG` with the wrong signature gives no evidence;
  - a compiled file whose source is gone gives no evidence;
  - Maven's local repository is never trimmed;
  - a cache entry newer than `cache_keep_days` is never trimmed;
  - in a team, a checkout-root entry matching today's name patterns, a folder in
    `.worktrees/` and an agenttalk-named entry in the system temp folder are reported
    and never removed.
- **Recheck:** a file touched after the report is skipped at apply time.
- **Links:**
  - a link nested inside an otherwise eligible run folder is removed as a link, and
    its target is untouched;
  - a candidate that is itself a link is refused;
  - a worktree registered outside the team root is neither committed, pruned nor
    removed;
  - the Windows fallback with a junction nested inside a stubborn folder leaves the
    junction's target untouched.
- **Trash:**
  - a removal leaves a slot whose `origin.json` names its origin and evidence;
  - each interrupted-move state is resolved as described, and the unclear ones are
    kept;
  - a restore onto an existing path, or through a link, is refused;
  - a file put into `trash/` by hand is never purged;
  - a move to the trash is reported as "not freed";
  - `--purge-trash` and `--no-trash` need the owner's or the operator's permission;
  - the total budget counts the trash.
- **Tools at each boundary:**
  - `JAVA_HOME` and `MAVEN_HOME` reach an ordinary child;
  - they reach the gateway-backed child only when they lie under `tools/`;
  - they never reach the dev gate or the comprehension worker;
  - no option string passes a filtered boundary unchanged.
- **Free space:** the checks cover the drives of the team root, the user's home and
  the user's temp folder, each once, wherever they are redirected.
- **The report:**
  - an observation older than its lifetime, with an unreadable time, or more than
    five minutes in the future is "unknown";
  - `state/` over budget gives a warning and nothing else;
  - an unreadable folder makes the size and the state "unknown", never zero or
    contained, and a scan that stops early says so.
- **The comprehension worker:** it writes no compiled files, and its temp folder is
  the run folder.
- **The migration step**, as an operations checklist, not a CI test:
  - a link in the source stops the copy;
  - a missing or changed file stops it;
  - each tool's smoke command must pass before the originals are retired.

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

If any boundary does not complete, the canary lists it under `failures` and exits
non-zero. Locations are named by kind only, never by full local path. The raw outputs
are `tests/support/team_folder_canary/evidence/windows-py3.10.json` and
`windows-py3.14.json`. Both Python versions gave the same picture, with no failures.

| Child | Mode | Temp folder (configured) | Temp file written (observed) | Compiled file written (observed) | Compiled file for installed code (configured) | pip cache (resolved) | `JAVA_HOME`, `MAVEN_HOME` |
|---|---|---|---|---|---|---|---|
| wrapper process | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | not set |
| ordinary child | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | not set |
| gateway child | baseline | user temp | not written | next to the probe's code | next to the installed program | project folder | not set |
| comprehension child | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | not set |
| dev gate | baseline | its run folder, in user temp | not run | not run | not run | not run | not run |
| wrapper process | team | `tmp/` | `tmp/` | `cache/pycache/` | `cache/pycache/` | `cache/pip/` | `tools/` |
| ordinary child | team | `tmp/` | `tmp/` | `cache/pycache/` | `cache/pycache/` | `cache/pip/` | `tools/` |
| gateway child | team | `tmp/` | `tmp/` | next to the probe's code | next to the installed program | project folder | dropped |
| comprehension child | team | `tmp/` | `tmp/` | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | dropped |
| dev gate child | team | its run folder in `tmp/` | run folder | run folder | next to the installed program | turned off | dropped |

Notes on the table:
- The canary's probe code lives in the team root's `work/` folder, so "next to the
  probe's code" lands inside the team root. For code a real child imports, such as
  the installed agenttalk, the "installed code" column applies.
- The canary predates the run folders proposed above, so its `TEMP` is `tmp/` itself.
- In team mode it sets `JAVA_HOME` and `MAVEN_HOME` to folders under `tools/`, only to
  see which boundaries pass them on; it runs neither tool.
- Baseline mode writes no temp file and does not run the gate, because both would
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
cannot see a file made and removed during the run, a write inside a folder that
already existed, or an overwrite. It says nothing about who made a new name.

Each listing is bounded, by default at 200,000 entries and 60 seconds; a listing that
stops at a limit, or fails, makes the result unknown. In the recorded runs, both
listings succeeded. On Python 3.14 no new top-level name remained. On Python 3.10 one
remained, and its name was not one the canary uses; whose it is, is unknown.

Not verified: Linux and macOS, the real AI tools, git, Java, Maven, Node and npm.

## Technical details

- **Existing code this design builds on:**
  - `src/agenttalk/scratch.py`: `resolve_scratch_root` falls back silently to
    `default_scratch_root`.
  - `src/agenttalk/janitor.py`:
    - `JanitorConfig.tmp_root` defaults to the system temp folder;
    - `find_candidates` ages temp and scratch entries by their newest file;
    - `is_dirty_worktree` uses `git status --porcelain`, which hides ignored files;
    - `wip_commit_dirty_worktree` and the prune step are the changes that need the
      link check;
    - `remove_stubborn`'s Windows fallback runs `takeown /R`, `icacls /T` and
      `robocopy /MIR` without `/XJ` on a folder, which can reach through a nested
      junction.
  - `src/agenttalk/supervisor.py`:
    - the generated `Launch` and `Launch-Spec` apply `AGENTTALK_ROOT`,
      `AGENTTALK_PY`, `AGENTTALK_SCRATCH` and `CODEX_HOME` first, then `$a.env` or
      `$spec.env`;
    - gateway-backed seats refuse `$a.env`;
    - `New-WrapperLogTargets` keeps `$WrapperLogGenerations` generations.
  - `src/agenttalk/wrapper/run.py`: `_child_env` is an allowlist for the
    gateway-backed backend and passes everything for the others.
  - `src/agenttalk/dev_gate.py`: `_base_env` (whose list has no tool settings),
    `_default_external_base` and `execute_gate`; run-folder retention is #338.
  - `src/agenttalk/comprehension/worker.py`: `_ALLOWED_ENV_VARS`,
    `sanitized_worker_env` and `_worker_subprocess_argv` (`-s -S`).
  - `src/agenttalk/ovh_gateway_service.py`: `_base_gateway_environment`.
  - `src/agenttalk/powershell_host.py`: `_attach_kill_on_close_job`, agenttalk's
    existing Windows job-object primitive. The per-run job is a variant: named after
    the run, no kill on close, no breakaway.
  - `src/agenttalk/wrapper_logs.py`: `default_wrapper_log_root`; the ring is bounded
    by `AGENTTALK_WRAPPER_LOG_MAX_BYTES`.
  - `src/agenttalk/turn_events.py`:
    - `default_turn_events_root`, with `AGENTTALK_TURN_EVENTS_DIR`;
    - the per-seat cap `TURN_EVENTS_MAX_BYTES`, enforced by `_enforce_cap`;
    - `_prune_old_status`.
  - `src/agenttalk/capacity.py` (`read_claude_context_sidecar`) and
    `src/agenttalk/checkpoint.py` (`collect_context`).
  - Process identity: the start-token helpers agenttalk already has
    (`supervisor._start_tokens_match`, `supervisor_lifecycle.start_tokens_match`).
- **Canary:**
  - `tests/support/team_folder_canary/canary.py`: the launcher, the four inner runs,
    the bounded user-temp listing and the failure list;
  - `probe.py`: standard library only; pip's answer is taken only from a successful
    run's standard output, as one absolute path;
  - `child.py`: it probes, then runs `tests/support/stub_cli.py`;
  - `tests/test_team_folder_canary.py` pins the evidence rules: the pip rule, the
    unknown listings and the failure list.

  Run it with `python tests/support/team_folder_canary/canary.py --team-root <new
  folder> --out <file>`, with `PYTHONPATH=src`, `AGENTTALK_ROOT` unset and no
  gateway variables set.
- **Issue and challenge:** #336; challenge `ch-ac2ae695-441c-46b2-8605-747c3b040474`
  (reshape, accepted). Dev-gate run folders: #338.
