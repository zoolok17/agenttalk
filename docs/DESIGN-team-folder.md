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
caches. Every seat the team starts gets its temporary files and caches pointed there.
Because putting files in one place does not stop a disk from filling, the design also
adds size limits, low-space warnings and a clean-up policy with a named owner, all
through the existing clean-up command. A **containment report** says, for each
location, whether it is only configured, actually seen, unknown or enforced, and it
never claims more than it knows.

This is not a security boundary. Every team still runs as the same Windows user, so
any program a seat starts can still write anywhere that user can. Moving the folders
of teams that already exist is a separate, later operation. Stage 1 moves only one
small thing: the build tools that one host's seats borrow from another project's
folder go into the team's own folder.

What this document decides: the team root's layout and settings, which variables each
kind of child process gets, how limits, warnings and clean-up work and who owns them,
the four states of the report, the declared exceptions, the change to the clean-up
command, and what stage 1 builds. What it leaves open is listed under "Open
questions".

## Why

On 2026-10-04 one host was down to about 1% free space on its system drive. Old work
was spread over the user's temp folder, a shared scratch folder with 2.8 million
files, and 18 stray test folders in the drive root. On a second host, two teams share
one Windows user, so they share its temp folder, the pip and npm download caches and
the AI tools' home folders. A clean-up there cannot tell whose files are whose.

Checking this design turned up two more causes:

- **The dev gate never removes its run folder.** Every local gate run leaves a full
  copy of the checkout, its test environments and its results in the temp folder,
  for good.
- **The clean-up command cannot see those folders.** Its list of temp-folder names
  says `agenttalk-gate-*`, but the gate names its folders `agenttalk-dev-gate-*`.

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

| Folder | What goes there | Kept? |
|---|---|---|
| `projects/` | The team's agenttalk project folders: each holds its `.agenttalk/` bus and its checkout. | Lasting; never cleaned. |
| `work/` | Work a seat wants to keep: review worktrees, reports, saved probe results. | Lasting; never cleaned automatically. |
| `tools/` | Programs the seats need on their path that are not installed for the whole machine, such as a build tool, a Java runtime or Node. | Lasting; never cleaned. |
| `scratch/<seat>/<task>/` | Each seat's throwaway work (`AGENTTALK_SCRATCH`). | Removed after a set age. |
| `tmp/` | Every seat's and child's temporary files (`TEMP`, `TMP`, `TMPDIR`), and the dev gate's run folders. | Removed after a set age. |
| `cache/` | Download and build caches: pip, npm, compiled Python files. | Rebuilt on demand; trimmed by size. |
| `state/` | agenttalk's own per-team records: wrapper logs, the turn journal. | Kept; trimmed by size. |
| `trash/` | Whatever the clean-up removed, held for a few days so it can be restored. | Deleted after a set age. |

"Inside the team root" never means "safe to delete". Only `scratch/`, `tmp/`,
`cache/` and `trash/` are ever cleaned, and only through the rules below.

### Settings

The team root is declared in each of the team's projects, in
`.agenttalk/config.json`, under a new `team` key:

```json
{
  "team": {
    "root": "D:/Teams/example",
    "strict": true,
    "budgets": {
      "tmp": {"bytes": "20 GiB", "files": 500000},
      "scratch": {"bytes": "20 GiB", "files": 500000},
      "cache": {"bytes": "30 GiB"},
      "state": {"bytes": "2 GiB"}
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
  reason. `agenttalk doctor` names the problem, the supervisor does not start the
  team's seats, and `agenttalk scratch root` exits with an error. Today an unusable
  scratch setting silently falls back to the shared sibling `atk-scratch` folder;
  strict mode never does.
- **Without `team`**, or with `strict` off, everything behaves as it does today.
- The existing `scratch` settings stay. In a team, the scratch root defaults to
  `<team root>/scratch`, and `scratch.root` must lie inside the team root.

## Where each kind of child gets its locations

A seat is a chain of processes, and the variables travel down the chain. The
challenge's point stands: changing the launcher alone is not enough, because two
links in the chain build their own environment.

The variables stage 1 sets, all inside the team root:

| Variable | Points to | Who reads it |
|---|---|---|
| `TEMP`, `TMP` | `tmp/` | Windows programs, Python, Node |
| `TMPDIR` | `tmp/` | Python everywhere; most programs on Linux and macOS |
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

### 1. Supervisor to seat (the wrapper process)

Today the supervisor's launch already sets `AGENTTALK_ROOT`, `AGENTTALK_PY`,
`AGENTTALK_SCRATCH`, `CODEX_HOME` and each seat's own `env` setting, then starts the
wrapper. A gateway-backed seat refuses a literal `env` setting.

Stage 1: the supervisor computes the team variables from the `team` setting and
applies them to every seat, gateway-backed seats included. This is a computed set,
not a literal `env` entry, so it does not open the gateway-backed seat's refusal. The
wrapper also applies the same computed set to itself when it starts. So a seat
started by hand, or restarted by any path, keeps its routing.

### 2. Wrapper to an ordinary model child

The wrapper passes its whole environment to the model's command-line tool, minus a
few of its own markers. Whatever the wrapper has, the tool and everything the tool
starts inherit.

Canary (Windows): with the team variables set, the child's temp folder, a file it
wrote, its compiled Python files and pip's cache folder were all inside the team
root. Without them, they were in the user's temp folder and the user's home.

Not observed: the real AI tools' own use of temp and caches, because no paid turn
ran. Node, which the Claude tool runs on, documents that it uses `TEMP`/`TMP` on
Windows and `TMPDIR` elsewhere; until a seat reports it, the report says "unknown".

### 3. Wrapper to a gateway-backed child

This child gets a deliberately small environment, because a paid outside worker must
not learn the operator's real home. It keeps `PATH`, the Windows system folders,
`TEMP`, `TMP`, two Python text settings and every `AGENTTALK_` variable. It drops
everything else. It sets `HOME` and `USERPROFILE` to the project folder, and
`LOCALAPPDATA` and `APPDATA` to folders below it.

Canary (Windows): its temp folder followed the team's `TEMP`. Its pip cache landed
in the project folder, which is inside the team root but mixed in with the bus and
the checkout. `TMPDIR` never reached it, so on Linux and macOS this child would
write to the system's shared `/tmp` (not measured there).

Stage 1: let `TMPDIR`, `PIP_CACHE_DIR`, `npm_config_cache`, `PYTHONPYCACHEPREFIX` and
`XDG_CACHE_HOME` through, each only when its value lies inside the team root. They
are paths the worker could already infer from `TEMP`, so they reveal nothing new.

### 4. The dev gate and its tools

The dev gate builds its own environment from a short list. It sets `TEMP`, `TMP` and
`TMPDIR` to its own run folder, turns pip's cache off and passes on `HOME`,
`USERPROFILE`, `LOCALAPPDATA` and `APPDATA`. It puts the run folder inside its own
process's temp folder, unless `--temp-root` says otherwise.

Canary (Windows), running a test the way the gate runs pytest:

- With the team's `TEMP`, the run folder, every test's temp folder, compiled files
  and pytest's own cache were all inside the team's `tmp/`.
- The gate runs Python in isolated mode, which ignores `PYTHONDONTWRITEBYTECODE` and
  `PYTHONPYCACHEPREFIX`. So compiled files are written next to the exported code,
  inside the run folder. That is harmless.
- Tests still see the user's real home folders. Which tests write there is unknown.

Stage 1: when a team root is set, the gate puts its run folder under `tmp/` itself,
not only through `TEMP`, so a gate run started from an ordinary shell lands there
too. The clean-up command learns the gate's real folder name, and the retention
rules apply to it.

### 5. Other children agenttalk starts

- **The comprehension worker:** its short list keeps `TEMP`, `TMP`, `TMPDIR` and the
  home variables, so it follows the seat.
- **Packaging checks:** they use Python's temporary folders, which follow the seat.
- **The gateway service:** a declared exception (below). It runs from its own
  scheduled task, with that task's temp folder.

## Limits, warnings and clean-up

Putting files together does not stop a disk from filling, so stage 1 adds three
things on top of the layout:

- **Free-space warnings.** `doctor`, the console and the supervisor check the free
  space on the drive that holds the team root and on the system drive, where the
  user's home and temp live.
  - Below `warn_free_percent`, it is an attention item for the team's owner.
  - Below `stop_free_percent`, the dev gate refuses to start, because it is the
    largest writer, and the supervisor raises an urgent item.
  - Running seats are never stopped for this: stopping them would lose work.
- **Budgets.** Each cleanable folder has a size and file-count limit. The clean-up
  report measures them with a bounded scan. A scan that hits its time or entry limit
  says "size unknown, scan stopped", never a guess. Over budget is an attention item.
  Applying the clean-up then removes the oldest throwaway entries until the folder is
  back under its limit; it never touches lasting folders.
- **Retention.** Temp entries older than `tmp_keep_days` and scratch tasks whose
  newest file is older than `scratch_keep_days` are removed, as the clean-up already
  ages them today. Caches are trimmed oldest first, by size. Everything removed goes
  to `trash/` first and is deleted after `trash_keep_days`, so a mistake can be undone.

**Who owns it.** The operator sets the policy in the `team` setting. The team's
`owner`, normally its lead seat, runs the clean-up report on its routine (daily) and
applies it when a budget is exceeded or at a quiet time. `doctor` and the console
show the state to everyone. There is one cleaner, the existing `agenttalk janitor`;
stage 1 adds no second one.

## The clean-up command (janitor) changes

1. **Only the team's own disposable folders.** In a team, it removes entries only
   from `scratch/`, `tmp/`, `cache/` and `trash/`, plus today's checkout-root and
   `.worktrees/` candidates. It never removes from the shared system temp folder:
   there it only reports agenttalk-named entries. It never touches another team's
   root, `work/`, `state/` or any `.agenttalk/`.
2. **Never through a link.** Today it removes a link itself and never its target.
   Stage 1 also refuses, and reports, any candidate whose real location lies
   outside the team root.
3. **Unsaved work in ignored files.** Today a worktree whose only changes are files
   git ignores counts as clean, so it can be removed without a save. That is weaker
   than keeping unsaved work: an ignored file can be a local settings file, a
   database or a seat's notes. Stage 1 sorts ignored files into two groups:
   - known rebuildable ones (`__pycache__/`, `*.pyc`, `.pytest_cache/`,
     `.ruff_cache/`, `.mypy_cache/`, `build/`, `dist/`, `*.egg-info/`);
   - everything else. A worktree with ignored files of the second kind is reported
     as holding unsaved ignored files and is never removed automatically.
4. **A way to keep a folder.** A `.agenttalk-keep` file in a folder stops the
   clean-up from removing it or anything inside it.
5. **Trash first.** Removal moves entries to `trash/`, on the same drive, before
   anything is deleted.
6. **The gate's folders.** The temp-folder name list gains `agenttalk-dev-gate-*`.

## The containment report

`agenttalk doctor` and the console gain a containment section. Each location gets
exactly one of four states:

| State | Meaning | Example |
|---|---|---|
| configured | agenttalk set this location to point inside the team root; nothing more is known. | A seat's `PIP_CACHE_DIR` in its launch settings. |
| observed | A file was actually seen there, inside or outside. | The wrapper's start record shows its temp folder inside; a scan finds agenttalk-named folders in the user's temp. |
| unknown | Nothing tells us. | The AI tool's own caches; tests writing to the real home folders during a gate run; anything on Linux or macOS until measured. |
| enforced | agenttalk itself refuses the alternative. | Strict mode refuses to start a seat whose locations resolve outside; the clean-up refuses to delete outside. |

Rules:

- **An unobserved location is never shown as contained.**
- **The report never claims universal containment.**
- **File writes by programs are never "enforced" in stage 1.** With one Windows user
  for every team, any program can still write anywhere that user can. A folder
  convention is not a security boundary, and the report says so in its header.
- **Where "observed" comes from:**
  - each wrapper's start record, with its resolved locations;
  - the clean-up report's scan of the team root;
  - a cheap scan of known outside places: the user's temp folder (agenttalk-named
    entries only), the top level of each drive root, agenttalk's per-user folders,
    and the old sibling scratch folder.
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
| Programs installed for the whole machine (Python, the agenttalk runtime) | Programs, not work. Tools that a team's launcher takes from another project's folder are not an exception: they move into the team's `tools/`. |

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
- clean-up follows a link outside its boundary;
- a restart loses the routing;
- the report labels an unobserved path as contained.

## Stated limits

- **Same user, no boundary.** The team root is a convention that agenttalk applies
  and reports on. It does not stop anything from writing elsewhere.
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

## Appendix: canary evidence (Windows)

The canary is `tests/support/team_folder_canary/canary.py`. It uses no model, no
network and no gateway. It starts three children the way agenttalk starts them, once
with today's environment ("baseline") and once with the team variables ("team"):

- **wrapper:** a wrapped seat's turn through the wrapper's real spawn path, using the
  stub model tool from the enforcement canary;
- **gateway:** a child with the gateway-backed environment, which only probes itself;
- **gate:** a pytest run with the dev gate's own export, environment and launcher.

It records locations by kind only, never by full local path. The raw outputs are
`tests/support/team_folder_canary/evidence/windows-py3.10.json` and
`windows-py3.14.json`. Both Python versions gave the same picture.

| Child | Mode | Temp folder | A temp file it wrote | Compiled files | pip cache |
|---|---|---|---|---|---|
| wrapper process | baseline | user temp | (not written) | next to the code | user home, `AppData/Local/pip` |
| ordinary child | baseline | user temp | (not written) | next to the code | user home, `AppData/Local/pip` |
| gateway child | baseline | user temp | (not written) | next to the code | project folder |
| dev gate | baseline | run folder in user temp | (not run) | (not run) | (not run) |
| wrapper process | team | `tmp/` | `tmp/` | `cache/pycache/` | `cache/pip/` |
| ordinary child | team | `tmp/` | `tmp/` | `cache/pycache/` | `cache/pip/` |
| gateway child | team | `tmp/` | `tmp/` | next to the code | project folder |
| dev gate child | team | run folder in `tmp/` | run folder | run folder | turned off |

Baseline mode writes no temp file and does not run the gate, because both would
write into the user's real temp folder. Its answers come from Python and pip
themselves.

What the wrapper process resolved for agenttalk's own folders, with the team
variables set:

- **inside `state/`:** the turn journal;
- **inside `tmp/`:** the clean-up command's temp folder and the status line's context
  file;
- **still in the user's `AppData/Local/agenttalk`:** the wrapper logs, the signing
  keys and backups;
- **in `AppData/Local/agenttalk-ovh`:** the gateway's secrets;
- **the scratch root:** still the default sibling folder next to the project, because
  it follows the scratch setting, not `AGENTTALK_SCRATCH`.

During the recorded runs, the user's temp folder gained no new entry at all. An
earlier run of the same canary saw one new entry there, with no canary or agenttalk
name. Some other program on the machine wrote it, and the canary cannot tell which,
so such entries count as unknown.

Not verified: Linux and macOS, the real AI tools, git and npm.

## Technical details

- **Existing code this design builds on:**
  - `src/agenttalk/scratch.py` (`resolve_scratch_root` falls back silently to
    `default_scratch_root`);
  - `src/agenttalk/janitor.py` (`JanitorConfig.tmp_root` defaults to the system temp
    folder; `DEFAULT_TMP_FAMILIES` has `agenttalk-gate-*`; `is_dirty_worktree` uses
    `git status --porcelain`, which hides ignored files);
  - `src/agenttalk/supervisor.py` (the generated `Launch` and `Launch-Spec` apply
    `AGENTTALK_SCRATCH`, `CODEX_HOME` and `$a.env`, and gateway-backed seats refuse
    `$a.env`);
  - `src/agenttalk/wrapper/run.py` (`_child_env`: an allowlist for the gateway-backed
    backend, everything for the others);
  - `src/agenttalk/dev_gate.py` (`_base_env`, `_default_external_base`, and
    `execute_gate`, whose run folder is never removed);
  - `src/agenttalk/comprehension/worker.py` (`_ALLOWED_ENV_VARS`);
  - `src/agenttalk/ovh_gateway_service.py` (`_base_gateway_environment`);
  - `src/agenttalk/wrapper_logs.py` (`default_wrapper_log_root` follows
    `LOCALAPPDATA` or `XDG_STATE_HOME`);
  - `src/agenttalk/turn_events.py` (`default_turn_events_root`, with
    `AGENTTALK_TURN_EVENTS_DIR`);
  - `src/agenttalk/capacity.py` (`read_claude_context_sidecar` reads
    `cc-ctx-<session>.json` in the temp folder);
  - `src/agenttalk/checkpoint.py` (`collect_context`).
- **Canary:**
  - `tests/support/team_folder_canary/canary.py` (launcher and the three inner runs);
  - `probe.py` (standard library only; runs inside each child);
  - `child.py` (the stub model tool: it probes, then runs `tests/support/stub_cli.py`).
  Run it with `python tests/support/team_folder_canary/canary.py --team-root <new
  folder> --out <file>`, with `PYTHONPATH=src`, `AGENTTALK_ROOT` unset and no
  gateway variables set.
- **Stage-1 build, file by file.** These are estimates of changed lines, tests
  excluded:
  - `src/agenttalk/team_folder.py` (new, about 250): reading and checking the `team`
    setting, strict refusals, the layout, the computed variables, the four-state
    classification, free-space and budget helpers;
  - `src/agenttalk/scratch.py` (about 40): the team's scratch root, and a strict
    refusal instead of the silent fallback;
  - `src/agenttalk/janitor.py` (about 200): team scope, trash first, sorting ignored
    files, the link-boundary refusal, the keep file, budgets as candidates, the gate's
    folder name;
  - `src/agenttalk/supervisor.py` (about 60): apply the computed variables to every
    seat, gateway-backed included; refuse to launch in strict mode with a reason;
  - `src/agenttalk/wrapper/run.py` (about 40): the wrapper applies the variables at
    start; the gateway-backed allowlist passes the checked team variables;
  - `src/agenttalk/wrapper_logs.py` and `src/agenttalk/turn_events.py` (about 25):
    default folders under `state/`;
  - `src/agenttalk/dev_gate.py` (about 30): default run folder under `tmp/`, and the
    low-space refusal;
  - `src/agenttalk/doctor.py` and the console's data (about 200): the containment
    report, exceptions, budgets and free space;
  - `src/agenttalk/cli.py` (about 30): config validation output and the report
    command;
  - outside this repository: the desktop host's launcher, changed at the migration
    step (tools path, `JAVA_HOME`, `MAVEN_HOME`, Codex writable roots);
  - docs: README "Where agenttalk keeps files", `docs/ops/scratch-hygiene.md`,
    `docs/DEV-GATE.md`, CHANGELOG.
  - Tests: about 700 lines. They cover the setting's checks, each child boundary (the
    canary as tests on all three systems), the clean-up's trash, ignored-file, link
    and keep cases, and the report's states.
  - In total, about 900 lines of product code. Proposed as four pull requests, in
    this order:
    1. the setting and clean-up safety (scratch, janitor);
    2. routing (supervisor, wrapper, gateway-backed child, dev gate);
    3. the report, budgets and warnings;
    4. the documentation.
- **Issue and challenge:** #336; challenge `ch-ac2ae695-441c-46b2-8605-747c3b040474`
  (reshape, accepted).
