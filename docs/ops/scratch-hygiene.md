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

Resolve and create it with:

```
agenttalk scratch root --for <agent>              # the agent's OWN root: <scratch_root>/<agent>
agenttalk scratch root --for <agent> --task <id>  # a task-scoped subdirectory: .../<agent>/<task>
```

A wrapped agent also finds the agent's own root pre-resolved in
`$AGENTTALK_SCRATCH` (the wrapper exports it at launch; a dispatch that
predates this feature, or a project without a `"scratch"` config key,
still works - the default root always resolves, config or not). It is
deliberately the AGENT root, not a task subdirectory: `$AGENTTALK_SCRATCH`
lives for the whole supervised session, so pinning it to one task
directory would make everything written there subject to that task's
staleness window even while the seat is still actively using it (a real
data-loss path measured during review - see the janitor's own staleness
rule below). Scope further with `--task <id>` explicitly for anything
that should age out on its own.

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
   repository root (allow-listed name families only, configurable),
   `.worktrees/` (**every** directory there is a candidate, regardless of
   name - that location IS the family, unlike the repository root), the
   OS temp root (a narrower, case-SENSITIVE, directories-only,
   age-windowed family list - the temp root is shared with every other
   program on the machine, so it is treated far more conservatively than
   the repository root), and the scratch root (a task directory whose
   NEWEST file anywhere in its tree, not the directory's own mtime, is
   older than `keep_days` - a directory's mtime does not change when a
   file nested inside it is edited). Reports, for every registered git
   worktree, whether it has ANY uncommitted change (tracked or
   untracked). A location the janitor could not even list (e.g. an
   ACL-denied directory) is reported as `FAILED to list`, never silently
   skipped or swallowed.
2. **`--apply`**: for each dirty worktree, commits ALL changes (tracked
   and untracked) as a WIP commit on the worktree's OWN branch - REFUSED
   OUTRIGHT (neither committed nor removed) on a default branch
   (`master`/`main` by default, configurable) or a detached `HEAD`. A
   detached-HEAD WIP commit would be reachable from no ref and become
   effectively lost the moment its directory is removed, so a detached
   worktree is refused the same way a default-branch one is, even though
   `git worktree add --detach` is Rule 2's own recommended form for a
   review worktree - commit it onto a real branch (or leave it) before
   closing the task if it must survive `--apply`. Removes the allow-listed
   candidates - a symlink or junction candidate is removed AS THE LINK
   ITSELF; its target is never touched, entered, or overwritten. Runs
   `git worktree prune`.
3. **Removals that fail** (e.g. sandbox-restricted ACLs on Windows, or a
   link that resists even a plain unlink): printed as `FAILED`, never
   silently skipped, with an elevated re-run hint (Windows only; the
   escalation itself - taking ownership and re-granting access - also
   only runs on Windows, only in `--apply`, and never against a
   symlink/junction). The command is idempotent: re-running after fixing
   permissions removes what's left.

The janitor never touches: `.agenttalk/` (the bus), tracked files, a
configured `"foreign"` folder under the temp root (`scratch.foreign` in
config - reported and kept, never removed), or the target behind a
symlink/junction candidate. Outside `.worktrees/` (where every directory
is in scope), it never removes something that doesn't match an
allow-listed name family.

`agenttalk doctor` warns when registered worktrees or scratch-family
candidates exist outside the scratch root, so this doesn't need to be
checked by hand.

## Rule 4: cleanup is part of "done"

A batch is not closed until the janitor report is empty (or its leftovers
are named in the close record). A close-out checklist for a slice or
release ends with: janitor run, worktree list short, `git status`
warning-free, and the OS temp root and repository root free of agenttalk
scratch.
