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

`<scratch_root>` is SHARED: every seat's clone writes under the same
root (one directory per seat, per Rule 1 above), so a batch `--apply`
pointed at any one seat's repository walks every OTHER seat's task
directories too, as plain filesystem trees. The janitor accounts for
this - see Rule 3's ownership check - but it means "my scratch root" is
really "everyone's scratch root"; never assume a stale-looking task
directory under it belongs to nobody just because it isn't yours.

## Rule 2: worktrees are registered, short-lived, and never dirty at close

- `git worktree add --detach <scratch>/wt-<sha> <sha>` for reviews; a
  branch worktree only while the task is open.
- Before a task closes: commit or stash anything worth keeping on the
  task's own branch, then `git worktree remove <path>` and
  `git worktree prune`.
- Evidence that must outlive the worktree is copied OUT to wherever the
  programme keeps evidence; the worktree itself is disposable.

## Rule 3: `agenttalk janitor` runs at every batch close

### Release a merged checkout

For the lead: after merging and running `agenttalk board verify-merges`, confirm
that nobody is still using the worktree. From the main repository, first read
`agenttalk janitor --release-report`. It shows `WOULD RELEASE` with a size in bytes
for eligible checkouts, and `KEPT` with a reason for the others. Then run
`agenttalk janitor --release <worktree-path>` for the one checkout you have finished
with (replace the placeholder with its path). Read the report again on the daily tick.
The report is a list to read, not a to-do list: a newly created checkout at the
remote tip also qualifies. Confirm its author has finished before naming a path.

Both commands fetch `origin` and use its advertised default branch. Release requires
the checkout's exact HEAD to be an ancestor of that fetched branch; a squash merge
is refused even if its files match. The branch and commits survive. The checkout must
be registered here, inside the repository, configured scratch root or configured
temporary root, clean, and free of untracked files. Ignored files are kept unless
they belong to the janitor's shared cache list: `__pycache__`, `.pytest_cache`,
`.ruff_cache`, or `*.egg-info`. A `.env`, nested `.agenttalk`, database or other
evidence prevents removal; move evidence to its lasting home yourself.
Index flags that can hide edits (`assume-unchanged` or `skip-worktree`) also prevent
release. Clear them and inspect the files yourself before retrying. Local replacement
commits cannot stand in for the remote's real ancestry.

Links or junctions anywhere inside the checkout or on its path are refused, as are
folders without usable file identity. Configure physical paths instead of aliases
(for example the real directory behind a linked temporary folder). Active lanes,
launch requests and retained supervisor launch/configuration references also keep
the checkout; retire those references before retrying. Existing Windows file locks
are checked before deletion. There is no in-use check on Linux or macOS; confirm
yourself that every user has stopped before releasing there. Relative or empty
working folders in launch records are ambiguous and prevent release; record the
absolute folders instead. Older lanes can have relative worktree paths: release
checks those against both the main repository and the calling folder, and checks
any saved canonical worktree path too. An unrelated lane does not block release;
a refusal names the lane and its record. Both `.agenttalk/supervisor-state.json` and the alternate
`.agenttalk/state/supervisor-state.json` location are checked. A failed release
returns a non-zero exit code and never uses force or makes a WIP commit. A failed fetch refuses both modes; report mode
changes only Git's fetched metadata and removes nothing.

Release bypasses Git's file-system monitor and untracked-file cache so stale
answers cannot hide edits. A nested repository is kept even inside a cache
folder, including when its metadata is spelled `.GIT`.

As with the ordinary janitor, run while no process changes the tree or starts using
it. Checks are repeated just before `git worktree remove`, but cannot prevent a
concurrent filesystem change inside Git's deletion. A new lock or I/O failure at
that point can leave a partly removed checkout: inspect it, close its users, and
retry only after checking the remaining files. There is no force-delete fallback.
A concurrent path change during Git's deletion can also delete files outside the checkout.

### Batch scratch cleanup

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
   OS temp root (a narrower, case-SENSITIVE family list, matching files
   and directories alike, every entry individually gated by an age window
   - `tmp_keep_days`, comfortably longer than a normal task/gate duration
   - the temp root is shared with every other program on the machine, so
   it is treated far more conservatively than the repository root), and
   the scratch root (a task directory whose
   NEWEST file anywhere in its tree, not the directory's own mtime, is
   older than `keep_days` - a directory's mtime does not change when a
   file nested inside it is edited). Reports, for every registered git
   worktree, whether it has ANY uncommitted change (tracked or
   untracked - but NOT ignored: `git status --porcelain` never lists an
   ignored file, so ignored content is checked separately before removal).
   A location the
   janitor could not even list (e.g. an ACL-denied directory) is reported
   as `FAILED to list`, never silently skipped or swallowed, named
   relative to its scanned root, with the system's reason and no file
   name repeated from the error.
2. **`--apply`**: only for dirty worktrees inside a candidate that has passed
   the keep checks and is about to be removed, commits ALL changes (tracked
   and untracked) as a WIP commit on the worktree's OWN branch. Other registered
   worktrees are left alone. The main checkout is never WIP-committed, even
   when reached through a link or a path containing `..`. Worktree identity
   uses the physical folder; the original path is retained for link checks.
   A linked scanned root still refuses deletion; use its physical path instead.
   A candidate inside a registered worktree is kept unless the whole worktree
   is a removal candidate; the janitor does not commit an active checkout just
   to delete one folder inside it.

   Worktrees with ignored content are kept unless every ignored file is inside
   a known cache directory: `__pycache__`, `.pytest_cache`, `.ruff_cache`, or
   `*.egg-info`. This shared list lives in `janitor.IGNORED_CACHE_DIR_PATTERNS`.
   A `.env` file or `.agenttalk` directory anywhere inside a worktree keeps it,
   even inside a cache or when not ignored by Git. The report gives the keep
   reason, and no WIP commit is made there. An unreadable contents check also
   keeps the worktree. Cache names are an explicit cleanup policy: do not put
   irreplaceable files inside those directories.

   A dirty worktree is REFUSED
   OUTRIGHT (neither committed nor removed) on a default branch
   (`master`/`main` by default, configurable), a detached `HEAD`, or if
   the commit itself fails for any reason - `git` unresolvable, `git add`
   or `git commit` returning a non-zero exit code (a refusing pre-commit
   hook, `commit.gpgsign` without a key, no configured user identity), or
   the worktree still showing as dirty immediately after the commit. A
   directory is only ever removed once its worktree is confirmed CLEAN,
   never on the strength of "the commit command was run" - never
   `--no-verify`: a refusing hook should keep the work, not be bypassed.
   A detached-HEAD WIP commit would be reachable from no ref and become
   effectively lost the moment its directory is removed, so a detached
   worktree is refused the same way a default-branch one is, even though
   `git worktree add --detach` is Rule 2's own recommended form for a
   review worktree - commit it onto a real branch (or leave it) before
   closing the task if it must survive `--apply`. A CLEAN detached
   worktree is not automatically safe either: if its HEAD holds a commit
   that no branch, tag, or remote-tracking ref contains, it is refused
   too, dirty or not - pruning a clean-looking worktree like that would
   drop the only ref keeping that commit reachable. Remote-tracking refs
   count deliberately: Rule 2's own recommended review-worktree form is
   routinely detached at a fetched PR head that lives only under
   `refs/remotes/origin/*` until it lands on a local branch, and without
   crediting that, an ordinary, disposable review checkout would be
   refused on every single run for as long as it exists.

   A refusal applies in BOTH directions: a scratch task directory that
   itself looks stale by age but CONTAINS a refused worktree (exactly
   Rule 2's own recommended `<scratch>/wt-<sha>` layout, nested a level
   or two under a task directory) is refused right along with it, not
   removed out from under the worktree the moment the refusal line
   prints. Ownership matters too, not just discovery: ANY tree holding a
   `.git` entry (a clone's own `.git` directory, or a worktree's gitdir
   FILE) that is not a registered worktree of THIS repository is refused
   outright - another seat's dirty worktree living under the shared
   scratch root (Rule 1), a standalone clone nobody registered anywhere,
   or even this repository's OWN worktree if its `.git/worktrees/<name>`
   admin entry has separately gone missing, is invisible to `git
   worktree list` here and gets exactly the same protection as if it
   were. This ownership check is NOT limited to the scratch root or the
   repository root - it applies inside `.worktrees/` too: that location's
   "every directory there is a candidate, regardless of name" design (see
   Report mode, above) means the family filter is absent, not that
   ownership stops mattering; an unregistered tree dropped there is
   refused exactly like one found anywhere else. When git itself cannot
   be trusted (unresolvable, or `worktree list` failing) the same
   refusal applies even more broadly: any directory the janitor cannot
   ask git about at all is refused if its own tree contains a `.git`
   entry anywhere, or if any part of that tree could not even be listed
   - staleness age never overrides either check. Removes the allow-listed
   candidates - a symlink or junction candidate is removed AS THE LINK
   ITSELF; its target (and anything behind it, `.git` or otherwise) is
   never touched, entered, or overwritten. Runs `git worktree prune`.
3. **Removals that fail** (e.g. sandbox-restricted ACLs on Windows, or a
   link that resists even a plain unlink): the path is KEPT, never
   forced, and printed under `FAILED` with the reason, named relative to
   the scanned root it was found in (`[repo]`, `[tmp]` or `[scratch]`,
   the roots on the report's first line). The janitor only ever uses the
   ordinary delete:
   - **Before scanning,** it checks the repo, scratch and temp roots themselves
     and every parent, starting at the drive or filesystem root. Each existing
     root and parent must be a plain folder with a readable file identity.
     A linked ancestor is refused even
     when it was there all along, including a relocated profile reached through
     a junction. The report names the failed ancestor; no root is scanned and
     no cleanup runs when this check fails. Configure the actual destination
     path if cleanup there is intended, rather than a path through a link.
     On macOS, `/var` is commonly a link: configure `scratch.tmp_root` with
     the physical temp path (usually under `/private/var`) and choose physical
     repo and scratch paths too. A share that supplies no file identity is
     refused; choose a filesystem that provides stable file identities.
     A root that does not exist yet is recorded as absent; if it appears before
     apply, cleanup is refused. The default scratch location is the sibling of
     the physical repository, even when the repository path contains `..`.
     Explicitly configured paths stay as written so their links remain visible.
   - **Before changes,** it checks those same roots and parents again. A changed
     identity, link or unreadable folder keeps the candidates and reports `FAILED`.
     These checks run before worktree commits, each delete and worktree pruning.
     The scan records the device and file number (`st_dev`, `st_ino`), so
     replacing a root or parent with another plain folder is refused too.
   - **Before each delete,** it checks with lstat, never following a
     link, three things:
     - **The folders above the candidate:** the scanned root and every
       folder below it, down to the candidate's parent, must still be
       plain folders. A folder link (junction, symbolic link or other
       reparse point) there keeps the candidate.
     - **A candidate that became a link:** one that was not a link when it
       was found and is one now is kept and reported.
     - **A candidate that was already a link:** if it was a link when it
       was found, the link itself is removed, and what it points to is
       left alone. If it is no longer a link, it is kept.

     The scanned root itself is identity-checked. Strictly below that root,
     this check still compares link or not-link status, rather than file
     identities. A check that fails keeps the candidate.
   - **The ordinary delete** removes a folder link found inside the
     candidate as the link itself, without entering it.
   - **When a delete fails,** the remaining files are left as they are
     after the failed attempt. Files removed before the failure stay
     removed, and read-only flags may have been cleared, since the
     ordinary delete clears them first.
   - **No takeover:** the janitor never takes ownership, grants itself
     permissions or mirrors an empty folder over a stubborn one. Those
     steps walk into a folder link nested in the tree and act on whatever
     lies behind it ([#342](https://github.com/zoolok17/agenttalk/issues/342)).
   - **Absent and removed** are reported only for a confirmed not-found.
     A path whose details cannot be read is `FAILED`.

   **The limit:** these checks cannot see a folder swapped for a link by
   another process during the delete itself. agenttalk is a trusted,
   single-user local tool, and the janitor runs while no other process
   rewrites that tree, so such a swap is out of scope.

   After this change is reviewed, merged and installed, `--apply` can be used again on paths
   whose roots and parents pass these checks, while no other process is moving or
   replacing those folders. Review the report first. Linked ancestors require
   an explicit choice of the actual destination path; they are never silently
   followed by the scan. This includes the protections against committing the
   main checkout through a link, deleting ignored work, and committing active
   worktrees that are outside the removal candidates.

   A refused scan or any `FAILED` item returns a non-zero exit code, including
   a failure reported during apply. Expected keeps (such as ignored work) are
   reported as `REFUSED (kept)` and are not removal failures.
   Look at each `FAILED` path, then remove it yourself. The command is
   idempotent: re-running after fixing permissions removes what's left.

The janitor never touches: the main checkout's `.agenttalk/` (the bus), its tracked files, a
configured `"foreign"` folder under the temp root (`scratch.foreign` in
config - reported and kept, never removed), or the target behind a
symlink/junction candidate. Outside `.worktrees/` (where every directory
is in scope), it never removes something that doesn't match an
allow-listed name family.

`agenttalk doctor` warns when registered worktrees or scratch-family
candidates exist outside the scratch root, so this doesn't need to be
checked by hand.

**Cost.** The ownership check (the `.git`-in-tree walk above) runs for
every directory candidate, including every registered worktree under
`.worktrees/` - a full checkout, not just its metadata - on every
`agenttalk janitor` run and therefore every `agenttalk doctor` run too.
Measured at roughly 1.8x the walk time of a build without it, on a
20,000-file stale task in one synthetic benchmark. Fine for ordinary
task/scratch sizes; worth revisiting (e.g. one shared walk instead of a
separate one per check) if `doctor` latency becomes noticeable on a
much larger tree.

**`refs/remotes` breadth.** A remote-tracking ref added from a
LOCAL-PATH remote (another seat's clone, added as a `git remote`) counts
toward reachability too. That local remote's own object store can later
be pruned or deleted, and a subsequent `fetch --prune` would drop the
only pointer keeping such a commit "reachable" as far as this check is
concerned. In the ordinary case - a real server remote (GitHub, etc.) -
the commit still exists upstream regardless of what happens locally, so
this is not a local data-loss path; it is called out here as a known
edge case of a local-only setup, not because it needs a different
default today.

## Rule 4: cleanup is part of "done"

A batch is not closed until the janitor report is empty (or its leftovers
are named in the close record). A close-out checklist for a slice or
release ends with: janitor run, worktree list short, `git status`
warning-free, and the OS temp root and repository root free of agenttalk
scratch.
