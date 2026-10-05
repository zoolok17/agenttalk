# Design: keep each team's work inside its own folder (team folder, stage 1)

Status: proposed. This is the design stage of #336, recast after the earlier design
(#337) was parked. Nothing in it is built yet. Where it describes how agenttalk works
today, that matches the code at the commit it ships with, and the canary in the
appendix measured it on Windows.

Audience: the operator who runs agenttalk teams on a machine, and the developers who
will build stage 1.

## In plain words

An agenttalk team leaves files in many places on a machine: the user's temp folder,
download caches shared with every other program, stray test folders and old run
folders. This design gives each team one folder, the **team root**, with fixed places
inside it for lasting work, throwaway files and caches. Every seat the team starts
gets its temporary files, caches, scratch work and agenttalk's own records pointed
there before it starts, each project in its own subfolder.

In stage 1, **agenttalk deletes and overwrites nothing in a team folder without a
person's command**, apart from a short, exact list of exceptions:
- the bus's own bookkeeping inside each project's `.agenttalk/`, as it is today;
- temporary files that one agenttalk operation makes only for itself and removes
  before it finishes, when nothing else can refer to them (no place in today's code
  qualifies);
- agenttalk's atomic replacement of its own status file.

Other programs, such as pip, Python, Maven and Node, manage their own files as they
always have. agenttalk cannot promise anything about them; the report shows what they
leave. The report shows what the folder holds: sizes, ages by the computer's clock,
owners, budgets and low-space warnings. When a person decides something can go, they
run one explicit command naming it, and it moves into a trash it can be restored
from. Automatic clean-up is a later design, because every safety problem the earlier
review found came from deciding on its own what may be deleted.

This is not a security boundary. Every team still runs as the same Windows user, so
any program a seat starts can still write anywhere that user can. Moving the folders
of teams that already exist is a separate, later operation; stage 1 moves only the
build tools that one host's seats borrow from another project's folder.

What this document decides:
- the team root's layout and settings;
- how projects in one team stay apart;
- which variables each kind of child process gets, and how they are checked before
  a process starts;
- how agenttalk behaves with no team, a team in warning mode and a strict team;
- exactly what agenttalk may still delete or overwrite in a team folder, and how a
  person removes something;
- what the report says and how it treats time;
- the declared exceptions;
- what stage 1 builds, with acceptance cases.

What it leaves open is listed under "Open questions" and "Not in stage 1".

## Why

On 2026-10-04 one host was down to about 1% free space on its system drive. Old work
was spread over the user's temp folder, a shared scratch folder with 2.8 million
files, and 18 stray test folders in the drive root. On a second host, two teams share
one Windows user, so they share its temp folder, the pip and npm download caches and
the AI tools' home folders. A clean-up there cannot tell whose files are whose.

The operator asked for all team work to stay in the team's folder, with exceptions
only where needed and never for general work or the message bus. The operator also
decided two things up front. First, the AI tools' home folders (settings and login)
stay in the user's home as a declared exception. Second, one Windows user per
machine, for now. Automatic deletion was not part of the ask.

An independent challenge (`ch-ac2ae695-441c-46b2-8605-747c3b040474`, verdict
"reshape", accepted) set the overall shape: a team root, checked at every child
boundary, with an honest report. The earlier design, #337, also proposed automatic
clean-up. Three review rounds kept finding ways for it to remove work still in use.
#337 is parked, and this design keeps only what passed review and leaves deletion to
a person.

Two related fixes are tracked separately:
- #338: the dev gate never removes its run folders (#344, in review, fixes it and
  also gives copied evidence a folder for each run);
- #342: today's clean-up command's Windows fallback can reach through a junction.

## The team root

### Layout

| Folder | What goes there |
|---|---|
| `projects/` | The team's agenttalk project folders: each holds its `.agenttalk/` bus and its checkout. |
| `work/<project id>/` | Work a project's seats want to keep: review worktrees, reports, saved probe results. |
| `tools/` | Programs the seats need on their path that are not installed for the whole machine, such as a build tool, a Java runtime or Node. |
| `scratch/<project id>/<seat>/<task>/` | Each seat's throwaway work (`AGENTTALK_SCRATCH`). |
| `tmp/<project id>/<seat>/<run>/` | One folder per wrapper run: that run's temporary files (`TEMP`, `TMP`, `TMPDIR`) and those of everything it starts. |
| `cache/` | Download caches shared by the team's projects (pip, npm, compiled Python files), and one Maven local repository per project, `cache/maven/<project id>/`. |
| `state/<project id>/` | agenttalk's own records for one project: wrapper logs, the turn journal. |
| `trash/` | What a person removed, with a record of where each item came from, so it can be restored. |
| `team.json` | The team's settings (below). |

In stage 1 agenttalk deletes and overwrites nothing in this layout without a person's
command, apart from the exceptions listed under "What agenttalk may still delete or
overwrite". What each folder holds is reported, and a person removes things with the
explicit command described under "Removing something".

### How projects in one team stay apart

Two projects in one team may well both have a seat called `lead` or `reviewer`. If
their folders were named only by seat, the two seats would write into the same
scratch folder, the same journal folder and the same run folders. The earlier review
showed this: a shared journal folder let one project's journal remove the other
project's records.

**Choice: every folder and record that one project owns carries the project's id.**
The alternative, refusing a team in which two projects use the same seat name, was
rejected for three reasons:
- **It already exists.** agenttalk already has a stable project id: the SHA-256 of
  the project folder's full path. It already names the wrapper-log, journal and
  signing-key folders after it. The collision the review found came only from a
  setting that replaced that whole folder.
- **Seat names repeat by nature.** Common seat names are reused across projects.
  Refusing them would force renames and turn adding a project into a launch failure.
- **No team-wide registry.** Unique names would need a register of every project's
  seats, which goes out of date as projects come and go. The id needs no register.

Where the id appears:
- the work folder, `work/<project id>/`;
- the scratch folder, `scratch/<project id>/<seat>/`;
- the run temp folder, `tmp/<project id>/<seat>/<run>/`;
- Maven's local repository, `cache/maven/<project id>/`;
- the project's records, `state/<project id>/`, holding its wrapper logs and its turn
  journal. `AGENTTALK_TURN_EVENTS_DIR` points at the project's own folder there,
  never at a folder two projects share;
- every owner record, which names the project id, the project folder, the seat and
  the run.

The pip and npm download caches, compiled Python files and the tools are the only
things the team's projects share, on purpose:
- pip and npm caches hold only downloads, which the tools fetch again on demand. A
  user's are shared by all of that user's projects today; sharing them within a team
  saves disk.
- Compiled Python files cannot clash, because each one's path follows the full path
  of the code it was compiled from.
- They hold no project's records. agenttalk deletes nothing in them, and the report
  never attributes anything in them to one project; it shows them for the team as a
  whole.

Maven's local repository is **not** shared. Besides downloads, it holds what a project
installs with `mvn install`, under the project's own group, artifact and version. Two
projects that install different builds under the same coordinates would replace each
other's files, even one after the other. So each project gets its own repository.
Sharing Maven downloads between projects is a later optimisation.

The project id changes if a project folder moves, because it is derived from the path.
The later folder move must carry each project's records over to its new id; stage 1
moves no project.

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
    "state": {"bytes": "4 GiB"},
    "trash": {"bytes": "10 GiB"}
  },
  "low_space": {"warn_free_percent": 10, "stop_free_percent": 3},
  "report": {"observation_max_wall_clock_age_minutes": 1440},
  "tools": {"java": "tools/java", "maven": "tools/maven", "node": "tools/node"},
  "owner": "example-lead",
  "exceptions": []
}
```

(Partial example of the proposed format, not runnable yet. The numbers are
placeholders for the operator to set, not recommendations.)

- **One copy for the whole team.** Every project of the team reads the same file, so
  budgets, the owner and the exceptions cannot differ between projects.
- **A team project** is any project whose config has the `team` pointer, whether or
  not the pointer and `team.json` are valid. That decides deletion behaviour (next
  section), so a broken setting can never switch automatic clean-up back on.
- **The existing `scratch` settings** stay for projects without a team. A team
  project's scratch root is `<team root>/scratch/<project id>/`.

### Behaviour with no team, a team in warning mode and a strict team

`strict` changes only whether a problem is a warning or a refusal. It never changes
what may be deleted.

| What | No team | Team, `strict` off | Team, `strict` on |
|---|---|---|---|
| Where seats' temp, caches, scratch and agenttalk's records go | Today's places | The team root, per project | The team root, per project |
| The pointer or `team.json` is invalid (missing root, no `team.json`, a root that is not its own folder, a subfolder a link takes outside, an exception without a reason) | Not applicable | Warning; seats start with today's environment | Refused: seats do not start, `scratch root` exits with an error |
| The routing check at a start fails | Not applicable | Warning recorded; the start goes ahead | The start is refused |
| A seat's own `env` or a recovery environment sets a routed name | Allowed, as today | Warning; the routed value wins | Refused |
| Free space falls below `stop_free_percent` | Not applicable | Warning | The dev gate refuses to start; urgent attention item |
| Deleting or overwriting without a person's command | Today's behaviour, except that the clean-up command never acts inside a team folder (any folder with a `team.json` at or above it) | **None**, apart from the listed exceptions, inside or outside the team folder | **None**, apart from the listed exceptions, inside or outside the team folder |
| The journal reaching its size cap | Removes its oldest files, as today | Stops, with a reported reason; removes nothing | Stops, with a reported reason; removes nothing |
| Old wrapper-log generations | Pruned, as today | Kept and reported | Kept and reported |
| A wrapper log reaching its size | Its fixed-size ring overwrites its oldest output, as today | A new file is opened; nothing is overwritten | A new file is opened; nothing is overwritten |
| The dev gate's finished run folders | As #338 decides | Kept and reported | Kept and reported |
| A lane's worktree and branch when a seat closes the lane | Removed, as today | Kept and reported; removed only by a person | Kept and reported; removed only by a person |
| Removing something | Today's commands | Only `agenttalk team remove`, run by a person | Only `agenttalk team remove`, run by a person |
| Emptying the trash | Not applicable | Only `agenttalk team purge-trash <slot>...`, run by a person | Only `agenttalk team purge-trash <slot>...`, run by a person |
| The containment report | None | Full | Full |

## Where each kind of child gets its locations

A seat is a chain of processes, and the variables travel down the chain. Changing the
launcher alone is not enough, because several links in the chain build their own
environment.

### The routed destinations

| Destination | Set by | Points to | Who reads it |
|---|---|---|---|
| Temp files | `TEMP`, `TMP` | `tmp/<project id>/<seat>/<run>/` | Windows programs, Python, Node |
| Temp files | `TMPDIR` | `tmp/<project id>/<seat>/<run>/` | Python everywhere; many programs on Linux and macOS |
| Java's temp files (Linux and macOS) | `JDK_JAVA_OPTIONS`, holding only `-Djava.io.tmpdir=<run folder>`, computed by agenttalk | `tmp/<project id>/<seat>/<run>/` | Java, which ignores `TMPDIR` there (on Windows it follows `TEMP`) |
| pip's cache | `PIP_CACHE_DIR` | `cache/pip/` | pip |
| npm's cache | `npm_config_cache`, and `cache=` in the team Node's own global `npmrc` | `cache/npm/` | npm |
| Compiled Python files | `PYTHONPYCACHEPREFIX` | `cache/pycache/` | Python |
| Other tool caches | `XDG_CACHE_HOME` | `cache/xdg/` | Linux and macOS tools that follow the XDG rules |
| Maven's local repository | `MAVEN_ARGS`, holding only `-Dmaven.repo.local=<folder>`, computed by agenttalk per project (Maven 3.9 or newer reads it) | `cache/maven/<project id>/` | the team's Maven; see "Maven's effective repository" |
| Scratch | `AGENTTALK_SCRATCH` | `scratch/<project id>/<seat>/` | seats |
| The turn journal | `AGENTTALK_TURN_EVENTS_DIR` | `state/<project id>/turn-events/` | the journal's writer |
| Wrapper logs | the supervisor's log targets | `state/<project id>/wrapper-logs/` | the supervisor and the wrapper |
| The tools | `JAVA_HOME`, `MAVEN_HOME` and the tools' `bin` folders first on `PATH` | `tools/` | Java and Maven builds, Node tests (set by the host's launcher; see "The desktop migration step") |

Configuring npm inside the team's own copy of Node, not only through a variable,
means that setting travels with the program into every child boundary, including the
ones that drop variables. For Maven the installation's own setting is only a default;
see "Maven's effective repository".

What stage 1 does **not** move: `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA`.
Moving them would also move the AI tools' logins, agenttalk's signing keys and the
gateway's secrets, which the operator decided stay per user (see "Declared
exceptions").

### One check for every reserved destination

One check validates every destination in the table above. It runs in three places:
the supervisor before it starts a seat, `agenttalk team run` before it starts a
command, and the wrapper as its first step. The check:

- **Temp:** `TEMP`, `TMP` and `TMPDIR` must all name the same run folder of this
  project's seat, and that folder's owner record must name this project and seat.
- **Caches:** `PIP_CACHE_DIR`, `npm_config_cache`, `PYTHONPYCACHEPREFIX` and
  `XDG_CACHE_HOME` must each name its own folder under `cache/`.
- **Per-project folders:** `AGENTTALK_SCRATCH` must name
  `scratch/<project id>/<seat>/`. `AGENTTALK_TURN_EVENTS_DIR` and the wrapper-log
  targets must lie under `state/<project id>/`, where the id is this project's.
- **Tools:** for each tool `team.json` declares, `JAVA_HOME` and `MAVEN_HOME` must lie
  under `tools/`, and that tool's `bin` folder must come before any other copy of it
  on `PATH`.
- **Computed options:** `MAVEN_ARGS` must hold exactly this project's computed value,
  and on Linux and macOS `JDK_JAVA_OPTIONS` must hold exactly its computed value.
- **In the wrapper:** Python's compiled-file folder must equal
  `PYTHONPYCACHEPREFIX`.

A missing or wrong value is reported by its name. In strict mode it refuses the
start; with strict off it records a warning (see the behaviour table).

### 1. Supervisor to seat: routing is set before the process starts

Python fixes its compiled-file folder when it starts, and its temp module remembers
the temp folder it chose the first time it is asked. Changing the variables inside a
process that is already running is therefore too late. So the variables must already
be in the environment when the wrapper's interpreter starts.

- **Launches and restarts.** For each launch, the supervisor does three things:
  - it creates a fresh run folder `tmp/<project id>/<seat>/<run>/`;
  - it writes an owner record into it, naming the project id, the project folder,
    the seat and the run;
  - it applies the routed variables.

  Requested restarts and recovery relaunches go through the same launch path.
- **Precedence.** Today the supervisor applies its own entries first, then the seat's
  own `env` setting (or, for a recovery launch, the recovery environment). Stage 1
  applies the routed set **last**, and the routed names are reserved (see the
  behaviour table). A gateway-backed seat already refuses a literal `env` setting; the
  routed set is computed, not literal, so that refusal stays as it is.
- **Hand starts.** A new command, `agenttalk team run --for <seat> -- <command>`,
  creates the run folder and owner record, sets the routed variables, runs the full
  check and starts the command.
- **The wrapper checks itself first.** Before it imports anything that uses temp
  files, the wrapper runs the full check on its own environment. If the check
  passes, it adds its process id and process start token to the owner record. The
  wrapper does not restart itself with a corrected environment: on Windows that would
  give it a new process id, and the supervisor tracks the one it started.
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
- With the routed variables set, the child's temp folder was inside its project's run
  folder. A file it wrote and a compiled file it wrote were inside the team root, and
  so was pip's resolved cache folder.
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
- pip's cache folder resolved into the project folder: inside the team root, but mixed
  in with the bus and the checkout.
- A compiled file for installed code would go next to the installed program.
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
- **`JDK_JAVA_OPTIONS` and `MAVEN_ARGS`** are computed again for the child, from its
  own `TEMP` and its project, never passed on.
- **npm's cache** also follows the team's own copy of Node through its settings file.

No option string is ever passed through this boundary as it is.

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
- **Run folder:** when a team root is set, the gate puts its run folder under the
  seat's run folder itself, not only through `TEMP`. In a team, the gate removes none
  of its run folders; they are kept and reported. #338 decides the behaviour outside a
  team. The committed-source copy the gate makes to start itself again is created
  inside that run folder and kept with it, instead of being removed when the gate
  finishes. In a team every run writes a new evidence bundle, and the gate refuses any
  destination that already exists (see the inventory below).
- **Tool settings:** deliberately not passed to the gate, which runs agenttalk's own
  Python checks and no Java, Maven or Node. A gate test that started Maven anyway
  would get the team Maven first on `PATH`, with its own settings file, but no
  `JAVA_HOME` or `MAVEN_HOME`. That case is unmeasured, and the report lists it as an
  exclusion.

### 5. The comprehension worker

The worker gets a fixed short list:
- it keeps `PATH`, the Windows system folders, the home variables, `TEMP`, `TMP` and
  `TMPDIR`, and sets `PYTHONPATH` to the agenttalk it runs;
- it drops `PYTHONPYCACHEPREFIX`, `PYTHONDONTWRITEBYTECODE`, the cache variables and
  the tool settings;
- it starts Python with `-s -S`, which do not stop compiled files from being written.

Canary (Windows):
- Its temp folder and a file it wrote followed the team's `TEMP`.
- A compiled file for installed code would go next to the installed program.
- pip's cache folder resolved to the user's home. The worker runs no pip.
- The tool settings were dropped.

Stage 1 policy:
- **No compiled files:** start the worker with `-B` as well, so it writes none. No
  new path crosses the worker's privacy list.
- **Tools:** the tool settings stay out, because the worker parses files and runs no
  tools. The report lists this as an exclusion.

### 6. Maven's effective repository

The repository a Maven run really uses is decided by the run, not by one setting:
- the installation's `conf/settings.xml` and the user's `.m2/settings.xml`;
- the project's `.mvn/maven.config`;
- the command line.

A project can commit a `.mvn/maven.config` with its own `-Dmaven.repo.local`, so a
setting in the installation alone does not fix the destination.

- **The supported invocation** is the team's Maven, from `tools/`, run with the
  `MAVEN_ARGS` agenttalk computed for the project and with no `-Dmaven.repo.local` of
  its own on the command line. Maven reads `MAVEN_ARGS` as part of its command line,
  which takes precedence over `.mvn/maven.config`, so the project's own repository
  should be the effective one. The acceptance test below confirms this for the
  team's Maven version before anything relies on it.
- **How it is checked.**
  - The routing check verifies `MAVEN_ARGS` at every start.
  - The migration step and a stage-1 acceptance test run the team's Maven offline with
    `-X validate` on a one-file project. Maven's debug output names the repository it
    uses, and it must be the project's own.
  - The acceptance test also runs it in a project whose `.mvn/maven.config` names an
    outside repository, and the project's own repository must still win.
- **The installation's `conf/settings.xml`** is only a default, for a run without
  `MAVEN_ARGS`. It names `cache/maven/unrouted`, which the migration step creates as an
  empty file, not a folder. A run that bypasses the routing should then fail with
  Maven's own error instead of writing a repository two projects could share. The
  migration step and a stage-1 test both confirm that Maven fails this way and writes
  nothing.
- **Unverified invocations** are listed as such in the report:
  - a project's own Maven wrapper (`mvnw`), which brings its own Maven;
  - any other Maven;
  - a run with `-Dmaven.repo.local` on its own command line;
  - IDE builds.

  The report does not claim where these write.

### 7. Other children agenttalk starts

- **Packaging checks:** they use Python's temporary folders, which follow the seat.
- **The gateway service:** a declared exception (below). It runs from its own
  scheduled task, with that task's temp folder.

## What agenttalk may still delete or overwrite

The claim, exactly: **agenttalk deletes and overwrites nothing in a team folder
without a person's command, apart from the exceptions below.** It makes no wider
claim than that. Other programs' handling of their own files (pip, Python, Maven,
Node, git, pytest) is not agenttalk's to promise. There is no exception for it; the
report shows what is there.

### The accepted exceptions

- **(a) The operator's.** The AI tools' home folders stay in the user's home, and
  there is one Windows user per machine.
- **(b) The bus's own bookkeeping inside each project's `.agenttalk/`, as it exists
  today and unchanged.** Stage 1 neither adds nor changes any of it. It is listed
  below so that nothing hides behind it.
- **(c) Scoped temporaries.** A file or folder that one agenttalk operation creates
  for itself and removes before that same operation returns. Each such site must meet
  all three conditions:
  - the operation itself created it;
  - nothing outside the operation refers to it: no other process, record, output,
    report or later step;
  - its removal never escalates and never follows a link.

  A site where a record, a later step or another process may refer to the contents
  does not qualify, and its files are kept. Copying them into durable evidence first
  does not make it qualify while anything still refers to the original location.
  Being new is not enough either: a folder made a moment ago can already hold a file
  that a hook or another process wrote and something else refers to. **No site in
  today's code outside `.agenttalk/` qualifies.** The failed lane setup was considered
  and is kept instead (see the table).
- **(d) Self-replacement.** agenttalk's atomic replacement of its own single-writer
  state file by a newer version of the same file. Outside `.agenttalk/` the only such
  file is the turn journal's status snapshot. It never covers logs, journals, evidence,
  scratch, work or caches.

### Inventory: inside a team folder, outside `.agenttalk/`

Every place where agenttalk, without a person's command, removes, renames over,
truncates or overwrites a file in a team folder outside `.agenttalk/`. The list
includes clean-up done by helpers and context managers, rollback after failures, and
replacement writes, not only direct delete calls.

| Where (today) | What happens today | Stage 1 in a team |
|---|---|---|
| The clean-up command (`janitor.find_candidates`, `apply`, `remove_stubborn`) | Removes stale scratch tasks, the checkout's name patterns, `.worktrees/` folders and matching system-temp entries by age and name | Off for a team project, inside and outside the team folder; it reports only. It never removes anything inside a folder with a `team.json` at or above it, whichever project runs it. |
| Lanes: the teardown after a delivery (`_lane_finalize_delivery`, run by `lane deliver`) | `git worktree remove` on the delivered lane's worktree under `.worktrees/` | Kept and reported, with the lane recorded as delivered and its cleanup pending. Delivering is not a command to delete; a person removes the worktree later with `lane gc --delete` or `team remove`. |
| Lanes: `lane abandon` and `lane gc --delete` | `git worktree remove` on the lane's worktree under `.worktrees/`, and `update-ref -d` on its branch | Inside a seat's turn: kept and reported, with the lane recorded as closed. Run by a person outside a seat's turn, these commands are that person's command to remove, as with `team remove`. |
| Lanes: a lane whose setup failed (`_cleanup_failed_provision`) | Removes the worktree it had just created and the branch it had just made, if the branch still points at its starting commit | Kept and reported, with the lane recorded as failed setup and its worktree and branch named; a person removes them. Not (c): git judges a worktree clean without counting ignored files, so `git worktree remove` without `--force` can delete an ignored output that another record still names (a review probe showed exactly this). A checkout hook or another process can write into the new worktree before setup returns. And checking the branch, then running `update-ref -d`, is not one atomic step. |
| The supervisor's Claude settings seed (`supervise --seed-claude-settings`, run at every launch of a Claude seat) | Rewrites `<launch folder>/.claude/settings.json` in place, merging in the seat's permission mode | Written only when the file is missing, with exclusive creation. When it exists and already holds the seat's mode, it is left as it is. When it holds another mode or cannot be read, the start check names the file: warning mode starts the seat with the file unchanged, strict mode refuses the start, and a person edits the file. |
| Lanes: the worktree-root marker (`.worktrees/` marker file) | Written again on every lane start | Written only when missing; never rewritten. |
| The turn journal's size cap (`_enforce_cap`) | Removes the oldest segment files | Off. The journal stops at its cap with the reason "size cap reached". |
| The turn journal's old status files (`_prune_old_status`) | Keeps the newest four older status files and removes the rest | Off. All are kept. |
| The turn journal's status snapshot (`write_atomic`: write `<status>.tmp`, then rename it over `status-<generation>.json`) | Replaces its own current status file | Exception (d): agenttalk's own single-writer state file, replaced by a newer version of itself. |
| Wrapper logs: the fixed-size ring | Overwrites its oldest output in its own files | Off. A full file is closed and the next one opened; nothing is overwritten. |
| Wrapper logs: the `.pending` marker (`_confirm_wrapper_log_generation`) | The wrapper writes `.committed` and removes `.pending` | `.pending` is kept. `.committed` is created with exclusive creation, and an existing one is left as it is. `.committed` wins wherever both exist; the supervisor and the report read it that way. |
| The supervisor's old log generations (`New-WrapperLogTargets` pruning) | Removes generations beyond the configured number | Off. All are kept and reported. |
| The supervisor's failed log attempt (`New-WrapperLogTargets`, after `Protect-WrapperLogPaths` or the sequence write fails) | Removes the attempt folder it just created | Kept and marked failed (a `.failed` file), then reported. A recursive `Remove-Item` is not shown to be safe against links, so it does not qualify as (c). |
| The supervisor's discarded launch (`Discard-PendingWrapperLogTargets`) | Removes a pending generation whose launch did not happen | Kept and marked discarded (a `.discarded` file), then reported. |
| The dev gate's run folders (`execute_gate`, #338) | A new folder for each run, in which the gate only creates files: the export, logs, built packages, the dependency snapshot. Kept today; #338 adds removal | Kept and reported in a team. |
| The dev gate's committed-source bootstrap (`TemporaryDirectory` in the gate's re-entry) | Removed when the gate finishes | Created inside the gate's run folder and kept with it. |
| The dev gate's evidence bundle (`write_run_evidence`, `write_preflight_block_evidence`, `write_aggregate_evidence`) | The JSON goes to `--evidence`, or to a new default name, and replaces any file already there. Each check's log is copied to `logs/<check-id>.log` beside it, which overwrites the log an earlier run wrote into the same folder, even when that run used a different JSON name | Every run writes a new bundle: the JSON at a path that must not exist yet, and its logs in a new folder named for this run beside it. Every destination is created with exclusive creation; one that already exists is refused before anything is written, and nothing is renamed over. Neither the JSON nor its logs can ever fall under (d). Related: #344, in review, builds a per-run folder for the copied files; this policy holds whatever shape that takes. |
| Assurance build and install trees (three `TemporaryDirectory` uses in `assurance.py`) | Removed when each check finishes | Kept: created inside the run's temp folder and never removed. Whether a record or later step refers to their contents was not established, so they are not treated as (c). |
| The wrapper's start probe (new in stage 1) | Not applicable | Writes one small file in its run folder and keeps it. |
| `supervise --install-activity-hook` | Merges agenttalk's hook into the project's `.claude/settings.json` and rewrites it | Unchanged: a person's command only. |
| `transcript --out <file>` | Writes the transcript to the file the person names, replacing it | Unchanged: a person's command only. Without `--out` it writes inside `.agenttalk/`. |
| `team remove`, `team restore`, `team purge-trash` | Not applicable | A person's command only; each refuses inside a seat's turn. |

### Inventory: inside `.agenttalk/` (exception (b), unchanged)

These are the bus's own bookkeeping, listed by purpose with the modules that do it.
Stage 1 changes none of them:
- **Messages, cursors and thread state** (`store.py`). This covers waiting markers,
  leases, consumed markers, capped awaiting records, the intent audit with its age and
  size cap, lingering terminal intents, composing intents and quarantine files.
- **Atomic writes of state files** (`_atomic.py`). Each writes a temporary file and
  renames it over the target.
- **The supervisor's own state** (`supervisor.py`, `supervisor_lifecycle.py`):
  - `supervisor-state.json`, `supervisor.json`, the chosen PowerShell host, its event
    log and its coordination and launch-barrier observations; its atomic replace uses
    a `.bak` file, which it then removes;
  - its process snapshots (`supervisor-snapshot.json` and each launch's barrier,
    pre-launch and post-launch snapshot), rewritten at each launch;
  - the scripts that `supervise init` and `supervise --refresh-scripts` generate.
- **Codex seats' own homes** (`.agenttalk/codex-home/<seat>/`, written by the
  supervisor and `codex_config.py`): at each launch the settings file is copied fresh
  from the operator's Codex settings and overlaid with the seat's settings; the login
  file is linked, or copied when it is missing.
- **The wrapper's own records** (`wrapper/obligations.py`, `wrapper_runtime.py`,
  `wrapper/session.py`): the owed-action ledger and its checkpoint, the runtime
  record and the session record.
- **Reply drafts and markers** (`wrapper/loop.py`, `reply_transport.py`,
  `reply_refusals.py`): the draft removed after delivery, the notice markers for stray
  drafts, and the records of refused replies.
- **Dead letters** (`cli.py`): resolving one writes a sidecar record, and requeueing
  moves its files; both run only on a person's command.
- **Checkpoints, cursors and thread state** (`checkpoint.py`): history capped at its
  limit, and the atomic `latest`.
- **Work-board facts** (`work_board_facts.py`) and **transcripts** written without
  `--out` (`transcript.py`, under `.agenttalk/sessions/`): atomic writes.
- **Lane deliveries** (`lanes.py`, `cli.py`): prepared artifacts, rejected finals moved
  to quarantine, and the integrity key.
- **Acceptance records** (`acceptance.py`): pending blobs linked or renamed into place.
- **Gates and closes** (`gates.py`, `close.py`): atomic state writes.
- **Comprehension** (`comprehension/lock.py`, `publish.py`, `staging.py`, and the
  `discovery.py` platform probe under `.agenttalk/comprehension/`). This covers locks,
  staged runs renamed into place, replaced runs and the case-sensitivity probe.
- **Assurance run records** under `.agenttalk/assurance/runs/`. These are new files
  only, never overwritten.

### Outside the team folder

Stage 1 does not change these, and most run only on a person's command:
- the AI tools' homes, Codex settings and installed skills (exception (a)), including
  what `install-skills` and `codex-config` write there;
- agenttalk's per-user signing keys and backups (`signing.py`, `recovery.py`);
- the gateway's per-user files (`ovh_gateway.py`, `ovh_gateway_service.py`).

### Writing into a team folder

This is the rule behind the table above, and every row meets it. Outside
`.agenttalk/`, every write agenttalk makes in a team folder without a person's command
does one of two things:
- it creates a file or folder that did not exist: with exclusive creation, so a file
  that appears between a check and the write is not replaced either, or inside a
  folder the same operation has just created for itself;
- it appends to agenttalk's own journal or log file.

It never truncates, replaces or renames over an existing file, apart from exception
(d), which outside `.agenttalk/` covers only the journal's status snapshot. Where a
destination already exists, agenttalk does not write over it. It either refuses the
operation, as the gate does for every part of its evidence bundle, or leaves the file
as it is and reports it, as with the worktree-root marker and the Claude settings
seed.

### The inventory test

Stage 1 adds a test that keeps this list true. **It is proposed here, not built:
nothing in this design shows it working yet.** It finds every call in agenttalk that
removes, renames over, truncates or overwrites a file, whether directly or through a
helper:
- **Python, removing:** the remove and unlink calls, `rmtree`, `rmdir`, and git's
  `worktree remove`, `update-ref -d` and `branch -d`/`-D`.
- **Python, replacing:** `replace`, `rename`, `shutil.move`, and copies onto a path
  (`copyfile`, `copy`, `copy2`, `copytree`).
- **Python, overwriting:** opening a path for writing or truncation (`open` with `w`
  or `r+`, `os.open` with `O_TRUNC`), `Path.write_text` and `Path.write_bytes`.
- **Python, temporaries removed on exit:** `TemporaryDirectory`, `NamedTemporaryFile`,
  and `mkstemp` or `mkdtemp` followed by a removal.
- **Through a helper:** a function that wraps one of these calls, such as
  `_atomic.write_text`, the journal's `write_atomic` or the supervisor's
  `_atomic_write_supervisor_json`, is listed as a primitive itself, so each of its
  callers needs an entry of its own.
- **The supervisor's generated PowerShell:** `Remove-Item` on files and folders,
  `Copy-Item` and `Move-Item` with `-Force`, `Set-Content` and `Out-File`,
  `[IO.File]::WriteAllText`, `WriteAllBytes`, `WriteAllLines`, `Delete`, `Replace`
  and `Move`, its own helpers that wrap them (such as
  `Invoke-StateFileSwapWithRetry`), and `robocopy`.

Each call must be listed in a checked-in file with its category:
- (b), (c) or (d);
- new only: it creates a file or folder that did not exist, with exclusive creation
  or inside a folder the same operation just created;
- off in a team;
- a person's command only;
- outside the team folder.

A call missing from the file fails the test. So does a category that does not hold,
for example a (c) site whose removal escalates, or a "new only" site that writes
without exclusive creation into a folder it did not create.

### Removing something: `agenttalk team remove`

A person removes something by naming it: `agenttalk team remove <path> --yes`. The
command:

- **Who.** It refuses when it detects that it runs inside a seat's turn (the
  wrapper's markers are in its environment). That stops a seat from running it by
  accident; it is not a security boundary.
- **What.** It accepts only a path inside the team root's `tmp/`, `scratch/`,
  `cache/`, `work/` or `state/`. It refuses `projects/`, `tools/`, `trash/`,
  `team.json`, the team root itself and those five folders themselves (it removes
  things inside them, never the folder).
- **First, it shows** the path's size, its file count, the wall-clock time of its
  newest file, any owner records inside, and any git worktrees inside with their
  state.
- **It refuses:**
  - a folder holding a worktree with uncommitted changes, or with commits no branch,
    tag or remote-tracking ref holds; the person deals with that worktree first;
  - a path that is itself a link, or whose way from the team root passes through a
    link;
  - a path whose real location is outside the team root.
- **Then it moves** the path into the trash, as below. A move on the same drive
  follows no link inside the path; links inside move as links.

### The trash

Moving something to the trash must keep it restorable, so the trash records where each
item came from.

- **Layout.** Each removal gets its own slot, `trash/<time>-<id>/`, holding:
  - `payload/`: the moved file or folder;
  - `origin.json`: where it came from (its path relative to the team root), what it
    was, who removed it and with which command, its size, when it was moved, and a
    state.
- **Moving.** The command writes `origin.json` with the state "moving" before it moves
  anything. It then renames the entry into `payload/`, a rename on the same drive, and
  finally sets the state to "moved".
- **Interrupted moves.** The report shows every slot still in "moving" and changes
  nothing. Only the next `team remove` or `team purge-trash`, run by a person, settles
  them, and it deletes nothing while doing so:
  - the entry is still at its origin and `payload/` is empty: the move never
    happened, and the state becomes "not moved"; the empty slot stays until a purge;
  - the entry is in `payload/` and nothing is at its origin: the move completed, and
    the state becomes "moved";
  - anything else (both, neither, or an unreadable record): the slot is kept and
    reported, and never purged.
- **Restoring.** An item is restored by moving `payload/` back to its origin, with
  `agenttalk team restore <slot>` or by hand following the same rules. Restoring
  refuses, and reports, when:
  - something already exists at the origin;
  - a folder on the way to the origin is a link, or lies outside the team root;
  - the slot's state is not "moved".

  Links inside the payload are moved back as links, never followed.
- **Emptying it.** `agenttalk team purge-trash <slot> [<slot>...] --yes`, run by a
  person, refuses inside a seat's turn in the same way. The person names each slot to
  delete; nothing is selected by age. The command first shows each named slot's
  origin, size, state and moved-at time. It then deletes only those whose
  `origin.json` is readable, whose state is "moved" (or "not moved", with an empty
  payload) and whose payload is intact. It deletes a payload without following any link
  inside it: each nested link is removed as a link first, and `robocopy`, if used, runs
  only with `/XJ`. Anything else in `trash/` is of unknown origin: a slot with a
  damaged record, or a file someone put there by hand. It is kept and reported, never
  purged.
- **Space.** Moving something to the trash frees no space on the drive. The command
  says so plainly, for example: "12 GB moved to the trash; no space was freed yet;
  `team purge-trash` frees it".

## Reporting what the folder holds

`agenttalk doctor` and the console gain a team section. It reports, it never deletes.

- **Contents.** For each folder under `tmp/`, `scratch/`, `cache/`, `state/`, `work/`,
  `tools/` and `trash/`, the report shows:
  - its size and file count, from a bounded scan. A scan that hits its time or entry
    limit, or cannot read a folder, says "size unknown", never a guess and never zero;
  - the wall-clock time of its newest file and that file's wall-clock age;
  - any owner records: project, seat, run, and the wrapper's process and start time;
  - for trash slots: their origin, state and age.
- **Budgets.** Each budget in `team.json` is compared with the measured sizes. Over
  budget is an attention item for the team's owner. A budget never deletes anything.
- **Free space.** The report checks the free space on each drive that matters:
  - the drive holding the team root;
  - the drive holding the user's home;
  - the drive holding the user's temp folder.

  Each drive is found from the actual folder, wherever it is redirected, and each is
  checked once. Below `warn_free_percent` it is an attention item. Below
  `stop_free_percent` it is urgent, and in strict mode the dev gate refuses to start.
  Running seats are never stopped, because that would lose work.

### The four states

Each location gets exactly one of four states:

| State | Meaning | Example |
|---|---|---|
| configured | agenttalk's settings or records say this location points inside the team root. No write there has been seen. | A seat's `PIP_CACHE_DIR` in its launch settings; the paths a wrapper resolved at start. |
| observed | A specific write or file was seen, at a recorded wall-clock time, within a stated scope. | The wrapper's start probe wrote its file inside its run folder at 09:14; a scan of `tmp/` that ran at 09:20 and finished found 3,210 files. |
| unknown | Nothing tells us, the record's time cannot be used, or a scan failed. | The AI tool's own caches; tests writing to the real home folders during a gate run; anything on Linux or macOS until measured. |
| enforced | agenttalk itself refuses the alternative. | Strict mode refuses a start whose routing check fails; `team remove` refuses a path outside the team root. |

Rules:

- **Configuration is never an observation.** Resolved paths, settings and start
  records are "configured", however exact they are.
- **An observation names its write, time and scope.** It comes from one of two
  sources:
  - the wrapper's start probe: right after its routing check, the wrapper creates one
    small file through Python's temp module, keeps it in its run folder, and records
    where it landed and the wall-clock time;
  - a report scan: each records when it ran, which folders it covered and whether it
    finished.
- **Found is not attributed.** A scan of outside places reports what it found. It
  attributes an entry to this team only when the entry carries agenttalk's own naming
  for this team and project: for example a run folder with this project's id. Anything
  else, including same-user names such as `pytest-of-<user>`, is "found outside, owner
  unknown". The outside places are the user's temp folder, the top level of each drive
  root, agenttalk's per-user folders and the old sibling scratch folder.
- **A failed scan is unknown**, never zero and never contained.
- **An unobserved location is never shown as contained**, and the report never
  claims universal containment.
- **Exclusions are listed.** Child boundaries that deliberately do not get a setting
  (the tool settings in the dev gate and the comprehension worker) are listed as
  exclusions, not as contained.
- **File writes by programs are never "enforced".** With one Windows user for every
  team, any program can still write anywhere that user can. A folder convention is not
  a security boundary, and the report says so in its header.
- **Declared exceptions are listed** with their reasons, owners and review dates.

### Time

Every time the report shows is the computer's wall-clock time, and every age is a
wall-clock age: the difference between two clock readings, labelled as such. It is not
proof of how much time really passed, and the report never calls anything "fresh".

- **A time that cannot be read is "unknown".**
- **A time more than five minutes ahead of the reader's clock is "unknown".**
- **Old observations.** An observation older than
  `report.observation_max_wall_clock_age_minutes` (default 1440, one day) of wall-clock
  age is shown as "unknown, last observed at <time>".
- **Clock changes are not detected.** If the clock was set back, an old observation
  looks younger than it is, and the report cannot tell. That is why ages are shown as
  wall-clock ages. Nothing in stage 1 makes a decision from an age: the purge command
  removes only the slots a person names.

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

**How a team adds one.** An entry in `team.exceptions` names what it is, where it is,
why, its owner and a review date. `doctor` lists it. An entry without a reason or an
owner is invalid (see the behaviour table). Never allowed as an exception: the bus,
checkouts, scratch, temp files and caches for general work.

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
   - Create `<team root>/cache/maven/unrouted` as an empty file, not a folder.
   - Set `localRepository` to exactly that file's path in the team Maven's
     `conf/settings.xml`. This is the installation default described under "Maven's
     effective repository": a run without the routing should then fail instead of
     writing a repository the team's projects would share. Step 6 checks that it does.
   - Set `cache=` to `cache/npm/` in the team Node's global `npmrc`.
   - Check that the user's own Maven settings (`.m2/settings.xml`) set no
     `localRepository`, and the user's own npm settings set no `cache`; either would
     win over these.
4. **Check the configuration.** Run the launcher's `-EnvOnly` check, which prints the
   environment it would give the seats and starts nothing. Every tool location must
   be inside the team root. This checks paths and settings only; it does not show that
   the tools work.
5. **Prove each tool works.** Run short offline smoke commands under the environment
   `-EnvOnly` printed, each with a time limit and none with network access (npm's
   update check turned off):
   - `java -version` and `javac -version`;
   - `mvn -v`, whose output must name the copied Maven and the copied Java;
   - `node --version`;
   - `npm --version`, and `npm config get cache`, which must print the team's npm
     cache.

   Every command must succeed.
6. **Check Maven's repository both ways**, offline, in a one-file project made for the
   check:
   - **Routed:** with the `MAVEN_ARGS` agenttalk computes for one project,
     `mvn -o -X validate` must name `cache/maven/<project id>/` as its local
     repository. It must do so again in a copy of that project whose
     `.mvn/maven.config` names an outside repository.
   - **Unrouted:** with `MAVEN_ARGS` unset, `mvn -o -X validate` must name
     `cache/maven/unrouted` as its local repository, and `mvn -o install` must fail.
     Afterwards `cache/maven/unrouted` must still be an empty file, nothing new may
     exist under `cache/maven/`, and the user's own `.m2/repository` must have gained
     no new entry (listed before and after).

   Either check failing stops the migration.
7. **Restart and test.** Restart the seats, then run the Node console tests once.
8. **Keep the originals until every check above has passed.** Pointing the launcher
   back is the rollback.

The same launcher now lets Codex seats write only inside the bus folder (their
"writable roots"). Once `tmp/`, `cache/` and `scratch/` exist, the migration step must
check what a Codex seat can still write there, and add those folders to its writable
roots if it cannot. The canary ran no Codex seat, so this is unverified.

## Not in stage 1

- **Automatic clean-up.** Deciding on its own that a folder may be removed comes in a
  later design. That design must answer what the review of #337 found:
  - **Earlier runs and their survivors.** A folder can still be used by a process
    from an earlier run, after a later run has ended.
  - **Readers, not only writers.** A folder can be read by a process that never
    writes to it.
  - **Project identity.** Every record it decides from must carry the project.
  - **The clock.** Wall-clock age is not elapsed time: a clock set back hides age. It
    needs an elapsed-time basis that survives restarts, or it must not decide from
    age.
  - **One rule in every mode.** What may be deleted must not depend on warning
    mode versus strict mode.
  - **The ideas #337 explored.** Per-run Windows jobs, explicit release of scratch and
    proof that every process of a run ended start there, not here.
- **Move existing folders.** One team today is spread over several sibling folders.
  Moving them is a separate operation for a quiet window, with:
  - an inventory of every folder and worktree;
  - a rollback plan;
  - proof afterwards that no work was lost: unpushed commits, uncommitted files and
    each seat's ability to resume its session;
  - each project's records carried over to its new project id.
- **Give each team its own Windows user.** That is the strongest separation Windows
  offers, at the cost of a login per team. It is the operator's decision, for later.
- **Sandbox programs.** Nothing stops a program from writing elsewhere.
- **Prove anything on Linux or macOS.** The canary ran on Windows only; stage 1 turns
  it into tests that run on all three systems in CI.

## Kill signals

Stop and rethink if any of these happens:

- agenttalk deletes or overwrites anything in a team folder without a person's
  command, outside the listed exceptions;
- a call that removes or overwrites appears in agenttalk without an entry in the
  inventory;
- `team remove`, `team restore` or `team purge-trash` follows a link, writes over
  something, or purges a slot of unknown origin;
- two projects share a per-project folder or record;
- a restart loses the routing;
- the report shows a location as contained, or as observed, without the write, time
  and scope that observation needs.

## Stated limits

- **Same user, no boundary.** The team root is a convention that agenttalk applies
  and reports on. It does not stop anything from writing elsewhere.
- **Disks still fill.** agenttalk removes nothing on its own beyond the exceptions,
  so a busy team's folder grows until a person removes something. Budgets and
  low-space warnings say when.
  The journal stops at its cap instead of removing its oldest files.
- **Wall-clock ages.** Ages are differences between clock readings. A clock that was
  set back makes things look younger, and the report cannot detect that.
- **Shared caches.** The team's projects share the pip and npm caches, which hold only
  downloads and are built for shared use. Maven repositories are per project.
- **Other programs' own files.** pip, Python, Maven, Node, git and pytest create and
  remove their own files as they always have, inside and outside the team folder.
  agenttalk does not control that; the report shows what is there.
- **Maven runs that bypass the routing are unverified.** A project's `mvnw`, another
  Maven, an IDE or an explicit `-Dmaven.repo.local` decide their own repository.
- **Unknowns remain.** These were not measured:
  - the real AI tools' temp and cache use (no paid turns ran);
  - git's, Node's and npm's internal temp use;
  - which tests write to the real home folders during a gate run;
  - everything on Linux and macOS, including Java's `JDK_JAVA_OPTIONS` route. That
    route also makes Java print a notice on its error output, which a stage-1 test
    must check does no harm.
- **Size scans have limits.** A very large folder can stop a scan early; the report
  then says the size is unknown.
- **The inventory test is a proposal.** It is specified here with the cases it must
  catch. Until stage 1 builds it, nothing shows that it finds every site.
- **Moving temp moves shared signals.** The Claude status line writes a small context
  file into `TEMP`, and the checkpoint hook reads it. Both run inside the seat, so
  they move together. A reader outside the seat would look in the wrong place.

## Open questions

1. Should `work/` get a budget, and with what number?
2. Should backups move into the team root, with a copy kept per user?
3. What should the default budgets be on each host?

## Stage 1: the build, with acceptance cases

Four pull requests, in this order. Each functional one carries its own tests. The
sizes are estimates of changed lines.

1. **The setting, project identity and the full deletion policy.** Everything in the
   inventory lands here, so no seat ever runs under half of it. Files:
   - `team_folder.py` (new): `team.json` and the pointers, the mode rules, the project
     paths and owner records, the routing check;
   - `janitor.py`: report-only for team projects, plus `team remove`, `team restore`,
     `team purge-trash` and the trash records;
   - `cli.py`: in a team, lane worktrees and branches kept after a delivery, after a
     failed setup and inside a seat's turn; the worktree marker written only when
     missing; the Claude settings seed written only when missing;
   - `turn_events.py`: stop at the cap, keep old status files;
   - `wrapper_logs.py`: no overwriting, `.pending` kept and `.committed` created
     exclusively;
   - `supervisor.py`: generations kept, failed and discarded attempts kept and marked;
   - `dev_gate.py`: run folders and the bootstrap copy kept, and a new evidence bundle
     for each run, with every existing destination refused;
   - `assurance.py`: build and install trees kept;
   - the inventory test and its checked-in list.

   Until pull request 2 lands, a project with the `team` pointer is refused when it
   starts a seat ("team folders are not complete in this version"), in both modes. Its
   deletion policy already applies.

   About 1,100 to 1,450 lines of product code, and 1,350 to 1,700 lines of tests.
2. **Routing.** Files:
   - `supervisor.py`: run folders and owner records, the routed set applied last, the
     reserved names, the routing check before launch;
   - `cli.py`: `team run`;
   - the wrapper's start check and start probe;
   - the gateway-backed child's list, with `JDK_JAVA_OPTIONS` and `MAVEN_ARGS`
     recomputed;
   - the comprehension worker's `-B`;
   - the dev gate's run folder placement;
   - `wrapper_logs.py`: its folder under `state/<project id>/`;
   - the canary turned into tests on all three systems.

   Seats of team projects may start from here on. About 600 to 800 lines of product
   code, and 800 to 1,000 lines of tests.
3. **The report.** Files:
   - `doctor.py` and the console's data;
   - sizes, ages, owners, budgets and free space;
   - the four states and the time rules;
   - the unverified Maven invocations.

   About 500 to 700 lines of product code, and 500 to 700 lines of tests.
4. **Documentation.** README "Where agenttalk keeps files",
   `docs/ops/scratch-hygiene.md`, `docs/DEV-GATE.md`, CHANGELOG.

In total, about 2,200 to 2,950 lines of product code and 2,650 to 3,400 lines of
tests. That is more than the first estimate of this recast, because the full deletion
inventory now lands in pull request 1, including the per-run evidence bundle and the
lane and settings sites added in the second fix round.

**Strict mode before the report exists.**
- After pull request 1, the mode rules apply to the setting and to deletion, and
  starts of team seats are refused.
- After pull request 2, they apply to starts as well.
- Until pull request 3, `doctor` shows one line about the setting and says that the
  team report is not available yet; nothing is shown as observed or contained.

**Acceptance cases.** Each must pass before its pull request merges.

- **Two projects with the same seat name:**
  - two projects of one team each start a seat called `beta`, and each gets its own
    scratch, run temp, work, journal, wrapper-log and Maven repository folders;
  - each project's owner records name its own project;
  - `team remove` on one project's folder leaves the other's untouched;
  - both share the pip and npm caches.
- **Maven:**
  - two projects install different bytes under identical group, artifact and version
    coordinates, and each project's repository keeps its own bytes;
  - in a project whose `.mvn/maven.config` names an outside repository, the team's
    Maven with the computed `MAVEN_ARGS` still uses the project's repository, as
    Maven's `-X` output shows;
  - a Maven run without `MAVEN_ARGS` names `cache/maven/unrouted` and fails on it.
    Nothing new appears under `cache/maven/` or in the user's own `.m2/repository`;
  - the report lists a run through `mvnw` as unverified.
- **The three modes:** each row of the behaviour table, for no team, warning mode and
  strict mode. In particular:
  - in both team modes nothing outside the exceptions is deleted or overwritten
    without a person's command;
  - an invalid setting in warning mode does not switch today's clean-up back on.
- **The inventory:** for every row of the inventory, the stage-1 behaviour shown in
  the table. In particular:
  - the clean-up command removes nothing for a team project, and nothing inside a
    `team.json` folder for any project;
  - closing a lane inside a seat's turn keeps the worktree and the branch;
  - a delivered lane keeps its worktree, recorded as cleanup pending, whoever ran
    `lane deliver`;
  - a failed lane setup keeps its worktree and branch and reports them. This holds in
    each of these cases: the worktree holds an ignored output that a separate record
    names, a file a checkout hook wrote, or a file another process wrote; or the
    branch moved after setup began;
  - the Claude settings seed creates a missing file, leaves a file that already holds
    the seat's mode byte for byte unchanged, and leaves a file with another mode
    unchanged too: warned about in warning mode, refused in strict mode;
  - the journal stops at its cap and keeps its old status files;
  - a full wrapper log opens a new file;
  - `.pending` stays beside `.committed`, and an existing `.committed` is not
    rewritten;
  - the supervisor keeps generations and failed or discarded attempts;
  - the dev gate keeps its run folders and its bootstrap copy;
  - **the dev gate's evidence bundles:** two runs write `a.json` and then `b.json`
    into one folder. Afterwards `a.json` still validates against its own logs, every
    byte unchanged. A run naming an existing JSON path, or whose per-run log folder
    already exists, is refused before it writes anything. The same holds for preflight
    and aggregate evidence, and two runs with default names in one folder keep separate
    logs;
  - assurance keeps its build and install trees;
  - the start probe's file stays in the run folder;
  - the report changes nothing, including slots of interrupted moves;
  - **the inventory test catches what it should:** for each family it must catch, one
    new call without an entry is added in turn, and the test must fail on each. The
    families are a direct `os.remove`, a `Path.write_text` onto an existing path, a new
    caller of `_atomic.write_text`, and, in the generated PowerShell, an
    `[IO.File]::WriteAllText`, a `Copy-Item -Force` and a `Remove-Item -Recurse`. The
    test must also fail on a (c) entry whose removal follows a link.
- **Startup routing:**
  - a supervisor launch, a requested restart and a recovery relaunch each get a new
    run folder with an owner record naming the project, with the routed set applied
    last;
  - for every reserved destination in turn, `MAVEN_ARGS` included, one missing value
    and one wrong value (including another project's folder) are each reported by
    name, both at launch and at wrapper start: refused in strict mode, warned about
    otherwise;
  - `team run` starts a wrapper by hand with routing;
  - Python's compiled-file folder in the wrapper equals the team's;
  - all of this on Windows, Linux and macOS in CI.
- **Removing something:**
  - `team remove` refuses inside a seat's turn;
  - it refuses a path outside the team root, one in `projects/`, `tools/` or `trash/`,
    a link, and a path reached through a link;
  - it refuses a folder holding a worktree with uncommitted changes or unreachable
    commits;
  - a link nested inside a removed folder moves as a link, and its target is
    untouched.
- **Trash:**
  - each removal leaves a slot whose `origin.json` names its origin;
  - each interrupted-move state is resolved as described, and the unclear ones are
    kept;
  - a restore onto an existing path, or through a link, is refused;
  - `purge-trash` deletes only the named, intact slots, and never a file put into
    `trash/` by hand;
  - a purge does not follow a junction nested in a payload;
  - a move to the trash is reported as "no space freed yet".
- **Tools at each boundary:**
  - `JAVA_HOME`, `MAVEN_HOME` and `MAVEN_ARGS` reach an ordinary child;
  - the gateway-backed child gets `JAVA_HOME` and `MAVEN_HOME` only when they lie
    under `tools/`, and a recomputed `MAVEN_ARGS`;
  - none of them reaches the dev gate or the comprehension worker;
  - no option string passes a filtered boundary unchanged.
- **The report:**
  - a scan that fails or stops at a limit gives "unknown";
  - an unreadable time, or one more than five minutes in the future, gives "unknown";
  - an observation past its wall-clock lifetime gives "unknown, last observed at
    ...";
  - ages are labelled as wall-clock ages;
  - a resolved path alone is never "observed";
  - an outside entry without this project's naming is "found, owner unknown";
  - free space is checked on the drives of the team root, the user's home and the
    user's temp folder, each once.
- **The comprehension worker:** it writes no compiled files, and its temp folder is
  the run folder.
- **The migration step**, as an operations checklist, not a CI test:
  - a link in the source stops the copy;
  - a missing or changed file stops it;
  - each tool's smoke command must pass before the originals are retired;
  - `cache/maven/unrouted` exists as an empty file and the installation's
    `localRepository` names it;
  - with `MAVEN_ARGS`, Maven's `-X validate` names the project's own repository, also
    against a `.mvn/maven.config` override;
  - without `MAVEN_ARGS`, Maven names the unrouted file, an install fails, and nothing
    new appears under `cache/maven/` or in the user's own `.m2/repository`.

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

Controls:
- **Ambient switches.** Switches in the starting environment that change what a child
  writes are cleared from both modes before the comparison: bytecode off, pip's cache
  off and pip's cache folder, the compiled-file prefix, npm's and the XDG cache
  folders. pip reads no config file in either mode.
- **The pip query's own control.** The pip query in every probe runs with no pip
  configuration file read. The control is set on the query itself, because the
  gateway-backed child and the comprehension worker drop the outer one. The evidence
  records it, and a test plants a hostile pip configuration behind both filtered
  boundaries. It checks that an uncontrolled query is switched off by it, and that
  the probe's query is not.
- **Contamination.** If a child still reports bytecode or pip's cache switched off
  where the canary did not switch it off, the run is labelled "contaminated" and exits
  non-zero. So does any boundary that did not complete.
- **Completeness.** All eight runs (four children, two modes) must be in the report,
  each with a result of its own. An inner run that crashes, times out or leaves an
  unreadable result is recorded as a failure with its reason, and the report is still
  written. Every probe record must hold its required measurements: the environment,
  the temp folder, both compiled-file destinations, pip's answer, and in team mode the
  files it wrote. Otherwise the run is "incomplete", its comparison is "unknown", and
  it exits non-zero. Deliberate omissions stay explicit: the baseline gate is not run,
  and the gate's own pip cache is off.
- **The canary's own compiled files.** The canary's processes, and the probe module
  its children import, write no compiled files, without changing what a child
  measures: each measurement runs with bytecode writing as its process started. The
  report counts the compiled files the checkout gained or changed during the run, and
  any such file fails the run. The programs the children start write as they always
  do: git may refresh the checkout's index when the gate reads its state, and the
  gate's pytest may write compiled files next to its installed code.
- **Interpreter flags.** The comprehension child gets the worker's whole option prefix
  up to `-m`, whatever options the worker adds.
- **Per-project paths.** In team mode the canary uses the per-project paths this
  design proposes, with each mode's project id.

Locations are named by kind only, never by full local path; project ids are shown as
`<project-id>`. The raw outputs are in `tests/support/team_folder_canary/evidence/`.
Each run started from a fresh checkout of commit 9120996d, which holds the same canary
scripts as this commit, with no compiled files in it. Each had a home folder and a
temp folder of its own, empty at the start, so the runs did not use the user's real
home and temp folders; "user temp" below is that run's own temp folder. Bytecode
writing was on, except in the ambient-switches run, which was started with
`PYTHONDONTWRITEBYTECODE=1` and `PIP_NO_CACHE_DIR=1` set; the canary cleared both.

| Evidence file | Python | Failures | Measurement | Comparison | New top-level names left in the temp folder | Of those, named like the canary's | Compiled files the checkout gained or changed |
|---|---|---|---|---|---|---|---|
| `windows-py3.10.json` | 3.10.11 | none | complete | clean | 0 | 0 | 0 |
| `windows-py3.14.json` | 3.14.2 | none | complete | clean | 0 | 0 | 0 |
| `windows-py3.14-ambient-switches-set.json` | 3.14.2 | none | complete | clean | 0 | 0 | 0 |

A test reads this table and checks every cell against the evidence files, and checks
the two summary sentences in this appendix against them too, so neither can drift from
what was recorded.

All three runs had no failures, a complete measurement and a clean comparison.

| Child | Mode | Temp folder (configured) | Temp file written (observed) | Compiled file written (observed) | Compiled file for installed code (configured) | pip cache (resolved) | `JAVA_HOME`, `MAVEN_HOME` |
|---|---|---|---|---|---|---|---|
| wrapper process | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | not set |
| ordinary child | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | not set |
| gateway child | baseline | user temp | not written | next to the probe's code | next to the installed program | project folder | not set |
| comprehension child | baseline | user temp | not written | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | not set |
| dev gate | baseline | its run folder, in user temp | not run | not run | not run | not run | not run |
| wrapper process | team | `tmp/<project-id>/...` | there | `cache/pycache/` | `cache/pycache/` | `cache/pip/` | `tools/` |
| ordinary child | team | `tmp/<project-id>/...` | there | `cache/pycache/` | `cache/pycache/` | `cache/pip/` | `tools/` |
| gateway child | team | `tmp/<project-id>/...` | there | next to the probe's code | next to the installed program | project folder | dropped |
| comprehension child | team | `tmp/<project-id>/...` | there | next to the probe's code | next to the installed program | user home, `AppData/Local/pip` | dropped |
| dev gate child | team | its run folder under `tmp/<project-id>/...` | run folder | run folder | next to the installed program | turned off | dropped |

Notes on the table:
- The canary's probe code lives in the team root's `work/` folder, so "next to the
  probe's code" lands inside the team root. For code a real child imports, such as the
  installed agenttalk, the "installed code" column applies.
- In team mode the canary sets `JAVA_HOME`, `MAVEN_HOME` and the computed `MAVEN_ARGS`,
  only to see which boundaries pass them on; it runs neither tool. Only the ordinary
  children received them.
- The pip column is pip's resolved answer. No download and no cache write happened,
  so it says where pip would write, not that it did.
- Baseline mode writes no temp file and does not run the gate, because both would
  write into the user's real temp folder.

In team mode, a wrapper's scratch and journal settings named the project's own
folders, `scratch/<project-id>/...` and `state/<project-id>/...`.

About the user's temp folder, the canary claims only what one listing at the start
and one at the end can show: the new top-level names still there at the end. It cannot
see a file that was made and removed during the run, a write inside a folder that
already existed, or an overwrite, and it says nothing about who made a new name. Each
listing is bounded at 200,000 entries and 60 seconds; a listing that stops at a limit,
or fails, makes the result unknown. In each run in the table both listings succeeded,
and no new top-level name remained in that run's own temp folder.

The evidence these runs replaced was recorded at e44ed44f, with the real user's temp
folder, which other programs were using at the same time. Its Python 3.10 run found
two new top-level names left there, neither named like the canary's: found, owner
unknown. That does not mean the canary made none; it means the canary cannot say who
made them. The runs above replaced that evidence because the canary itself changed in
this round.

Not verified: Linux and macOS, the real AI tools, git, Java, Maven, Node and npm.

## Technical details

- **Existing code this design builds on:**
  - `src/agenttalk/signing.py`: `project_id_for_root`, the SHA-256 of the resolved
    project path, already used for keys, wrapper logs and the journal.
  - `src/agenttalk/scratch.py`: `resolve_scratch_root` falls back silently to
    `default_scratch_root`.
  - `src/agenttalk/janitor.py`:
    - `find_candidates` and `apply` are the age and name rules that become report-only
      for team projects;
    - `is_dirty_worktree` and `worktree_head_reachable` serve the checks in
      `team remove`;
    - `remove_stubborn`'s Windows fallback is #342.
  - `src/agenttalk/turn_events.py`:
    - `default_turn_events_root`: `AGENTTALK_TURN_EVENTS_DIR` replaces the whole
      per-project folder today, which is why the team route points it at the
      project's own folder;
    - `_enforce_cap` and `_prune_old_status` are the removals switched off in a team.
  - `src/agenttalk/supervisor.py`:
    - the generated `Launch` and `Launch-Spec` apply `AGENTTALK_ROOT`,
      `AGENTTALK_PY`, `AGENTTALK_SCRATCH` and `CODEX_HOME` first, then `$a.env` or
      `$spec.env`;
    - gateway-backed seats refuse `$a.env`;
    - `New-WrapperLogTargets` prunes old generations beyond
      `$WrapperLogGenerations`;
    - each Claude seat's launch runs `supervise --seed-claude-settings --dir
      <launch folder>`, which `cmd_supervise` in `cli.py` turns into a rewrite of
      `.claude/settings.json`.
  - `src/agenttalk/cli.py`: `_cleanup_failed_provision` (failed lane setup),
    `_lane_finalize_delivery` (teardown after `lane deliver`), and the `abandon` and
    `gc --delete` lane commands.
  - `src/agenttalk/wrapper_logs.py`: `default_wrapper_log_root` and the fixed-size
    ring.
  - `src/agenttalk/wrapper/run.py`: `_child_env` is an allowlist for the
    gateway-backed backend and passes everything for the others.
  - `src/agenttalk/dev_gate.py`: `_base_env` (whose list has no tool settings),
    `_default_external_base` and `execute_gate`; `write_run_evidence` copies each
    check's log to `logs/<check-id>.log` beside the evidence file, and
    `write_preflight_block_evidence` and `write_aggregate_evidence` write the other
    two kinds of evidence file.
  - `src/agenttalk/comprehension/worker.py`: `_ALLOWED_ENV_VARS`,
    `sanitized_worker_env` and `_worker_subprocess_argv` (`-s -S`).
  - `src/agenttalk/ovh_gateway_service.py`: `_base_gateway_environment`.
  - `src/agenttalk/capacity.py` (`read_claude_context_sidecar`) and
    `src/agenttalk/checkpoint.py` (`collect_context`).
- **Canary:**
  - `tests/support/team_folder_canary/canary.py`: the launcher, the four inner runs,
    `run_boundary` (an inner run that fails becomes a recorded failure, never an
    exception), `base_environment` (clears the ambient switches), `contamination`,
    `incomplete` (checks the expected set of runs and their records), the bounded
    user-temp and checkout listings, and the failure list;
  - `probe.py`: standard library only; pip's answer is taken only from a successful
    run's standard output, as one absolute path;
  - `child.py`: it imports the probe without writing a compiled file, probes as the
    process started, then runs `tests/support/stub_cli.py`.
  - `tests/test_team_folder_canary.py` pins:
    - the pip rule and the pip query's own controls;
    - unknown listings;
    - the failure list, including inner runs that crash, time out or leave an
      unreadable result, with the report still written;
    - the expected set of runs;
    - the cleared switches;
    - the contamination label;
    - per-project paths for the same seat;
    - no compiled files written into a fresh checkout, by the command or by a child;
    - the appendix's evidence table, row by row, against the evidence files.

  Run it with `python tests/support/team_folder_canary/canary.py --team-root <new
  folder> --out <file>`, from a checkout nothing else is using, with
  `PYTHONPATH=src`, `AGENTTALK_ROOT` unset and no gateway variables set.
- **References:**
  - #336, the issue;
  - #337, the parked earlier design;
  - #338, the dev gate's run folders, and #344, its fix in review;
  - #342, the clean-up command's junction fallback;
  - challenge `ch-ac2ae695-441c-46b2-8605-747c3b040474` (reshape, accepted).
