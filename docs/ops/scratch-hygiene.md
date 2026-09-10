# Scratch hygiene: where temporary work goes, and how it gets cleaned up

Applies to every seat (wrapped agents, subagents, the lead) and every batch
of work (a slice, a PR round, a review sweep, a release) in any project run
over agenttalk.

## Why this exists

Unmanaged scratch has the same shape everywhere agenttalk runs: pytest
base-temps, review clones, git worktrees, service data directories, and
reply-draft probes land in the repository root, in the OS temp root, or in
`.worktrees/`, and nothing owns their removal. Left alone for long enough
this produces hundreds of registered git worktrees nobody remembers,
thousands of scratch entries in the shared temp root, worktrees holding
uncommitted work, and directories created inside a sandboxed process whose
ACLs deny even a listing until an elevated shell takes ownership. Nothing
owned the cleanup, so it never ran - `agenttalk` owns it now, because every
project has the same sprawl.

## Rule 1: one scratch root per seat and task

Every piece of temporary work lives under

    <scratch_root>/<seat>/<task-id>/

`<scratch_root>` defaults to a sibling `atk-scratch/` directory next to the
project root (`<project>/../atk-scratch`), and is configurable in
`.agenttalk/config.json`:

```json
{"scratch": "D:/custom/scratch/root"}
```

or the long form (any key may be omitted):

```json
{"scratch": {"root": "D:/custom/scratch/root", "keep_days": 3}}
```

Resolve and create the per-agent/per-task directory with:

```
agenttalk scratch root --for <agent> [--task <id>]
```

A wrapped agent also finds it pre-resolved in `$AGENTTALK_SCRATCH` (the
wrapper exports the per-seat root at launch; a dispatch that predates this
feature, or a project without a `"scratch"` config key, still works - the
default root always resolves, config or not).

Uses:
- pytest: `--basetemp <scratch_root>/<seat>/<task-id>/pt`
- review clones and worktrees: `.../<task-id>/wt-<sha>`
- service data directories (databases, brokers): `.../<task-id>/data-<n>`
- reply drafts and probe outputs: `.../<task-id>/`

Never: the OS temp root directly, the repository root, `.worktrees/`, or
another seat's root. A dispatch that asks for a build, gate, review, or
probe names the scratch root explicitly; a close-out that created scratch
reports "scratch removed" or lists what is left and why (a worktree kept
for a follow-up, a preserved evidence tree).

## Rule 2: worktrees are registered, short-lived, and never dirty at close

- `git worktree add --detach <scratch>/wt-<sha> <sha>` for reviews; a
  branch worktree only while the task is open.
- Before a task closes: commit or stash anything worth keeping on the
  task's own branch, then `git worktree remove <path>` and
  `git worktree prune`.
- Evidence that must outlive the worktree is copied OUT to wherever the
  programme keeps evidence; the worktree itself is disposable.

## Rule 3: `agenttalk janitor` runs at every batch close

```
agenttalk janitor                          # report only (default)
agenttalk janitor --apply                  # clean up
agenttalk janitor --apply --keep-days 7    # override the scratch-root staleness window
```

In order:

1. **Report mode** (default): lists scratch candidates under the
   repository root, `.worktrees/`, the OS temp root (allow-listed name
   families only, configurable), and the scratch root (past its
   `keep_days` window), with counts and the oldest entries - and, for
   every registered git worktree, whether it has uncommitted changes to
   TRACKED files (an all-untracked worktree is not "dirty" in this
   sense).
2. **`--apply`**: for each dirty worktree, commits the tracked changes as
   a WIP commit on the worktree's OWN branch - refused outright on a
   default branch (`master`/`main` by default, configurable), never
   auto-committed there. Removes the allow-listed candidates. Runs
   `git worktree prune`.
3. **Directories that refuse removal** (e.g. sandbox-restricted ACLs on
   Windows): printed as `FAILED`, never silently skipped, with an
   elevated re-run hint (Windows only; the escalation itself - taking
   ownership and re-granting access - also only runs on Windows, and only
   in `--apply`). The command is idempotent: re-running after fixing
   permissions removes what's left.

The janitor never touches: `.agenttalk/` (the bus), tracked files, or a
configured `"foreign"` folder under the temp root (`scratch.foreign` in
config - reported and kept, never removed). It never removes a directory
that doesn't match an allow-listed name family.

`agenttalk doctor` warns when registered worktrees or scratch-family
candidates exist outside the scratch root, so this doesn't need to be
checked by hand.

## Rule 4: cleanup is part of "done"

A batch is not closed until the janitor report is empty (or its leftovers
are named in the close record). A close-out checklist for a slice or
release ends with: janitor run, worktree list short, `git status`
warning-free, and the OS temp root and repository root free of agenttalk
scratch.
