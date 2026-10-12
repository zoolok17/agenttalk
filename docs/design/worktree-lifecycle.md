# Finished worktrees are cleaned up reliably

**In plain words:** Agents make a separate working copy of the project (a git "worktree") for each piece of work, and nobody reliably removes it afterwards. This design adds one central sweep that removes only the working copies that are certainly finished: the work is merged, nothing in the folder is unsaved, and nothing is using it. Agents never delete anything; at most they leave a small note saying they are done. The sweep keeps and reports everything it is unsure about, and it never touches the branch or the commits, only the folder. It is introduced in steps: first a number in `status` and `doctor`, then one command a person runs, and only later, with a team's written permission, a timer. This page also reports what we measured on the desktop first, because the numbers changed the plan: the working copies are a smaller part of the disk use than we assumed.

Status: design, for review. Nothing in this document is built. Date of the measurements: 2026-10-08, base commit `7e36cfb7`.

## 1. What we measured

The measurements were read-only. Nothing was deleted, moved or changed, `janitor --apply` was not run, and no git command that changes a worktree was run. Sizes count file bytes and do not follow links. Folders that the account cannot list are marked "not measured" and are not counted as zero.

### 1.1 The registered worktrees (98)

`git worktree list` in the repository shows 98 entries. One of them is the main checkout itself (2.50 GB), which is never a target. The other 97 are linked worktrees and total **3.07 GB**: about 32 MB each on average. All but one are clean. 52 of the 98 are detached (checked out at a commit, no branch), 46 are on a branch.

| Category (today's janitor rules) | Trees | Size | Notes |
|---|---:|---:|---|
| Removable under today's rules | 54 | 1.81 GB | in the scratch area, untouched for more than 3 days, clean, head on a ref |
| Kept only because they were touched in the last 3 days | 36 | 1.13 GB | would join the first row within days |
| Refused: dirty | 0 | 0 | none inside the scanned folders (one outside, see below) |
| Refused: detached and unreachable head | 0 | 0 | none inside the scanned folders (two outside) |
| Refused: unregistered or unlistable tree in the same task folder | 1 | 0.04 GB | |
| Outside the scanned folders | 7 | 2.59 GB | the main checkout 2.50 GB; two under `.claude/worktrees`; one `.wt-*`; three under `C:\tmp`, `C:\qwt` and the user temp folder |
| **Total** | **98** | **5.57 GB** | |

The tree sizes include ignored files. Ignored bytes in all 98 trees: 0.65 GB. Of that, 0.27 GB is in the main checkout (temporary review folders and similar) and 0.39 GB is in the linked trees. 95 of the 97 linked trees hold only regenerable caches (`__pycache__`, `.ruff_cache`, `.pytest_cache`) or no ignored file at all; the other two (under `.claude/worktrees`) hold a `.claude` folder of about 4 MB.

Who owns them (by folder): one reviewer seat 36 trees (1.15 GB), a developer seat 16 (0.48 GB), the author of this design 12 (0.45 GB), the lead 10 (0.35 GB), a frontend seat 8 (0.29 GB), another developer seat 8 (0.23 GB), a second reviewer seat 1, and 6 outside any seat folder besides the main checkout.

By owner folder and category (scanned folders only; the 7 trees outside them are in the table above):

| Owner folder | Removable today | Kept by age | Refused (sibling tree) |
|---|---:|---:|---:|
| reviewer seat 1 | 20 (0.66 GB) | 15 (0.45 GB) | 1 (0.04 GB) |
| developer seat 1 | 8 (0.24 GB) | 8 (0.23 GB) | 0 |
| the author of this design | 8 (0.31 GB) | 4 (0.15 GB) | 0 |
| the lead | 7 (0.24 GB) | 3 (0.10 GB) | 0 |
| frontend seat | 3 (0.13 GB) | 5 (0.16 GB) | 0 |
| developer seat 2 | 8 (0.23 GB) | 0 | 0 |
| reviewer seat 2 | 0 | 1 (0.04 GB) | 0 |
| **Total** | **54 (1.81 GB)** | **36 (1.13 GB)** | **1 (0.04 GB)** |

Of the 54 removable today, 41 are merged into `master`, 7 are in an open or closed pull request and 6 are on a branch with no pull request. Of the 36 kept by age, 25 are merged, 9 are in an open pull request and 2 are on a branch.

**How many are merged, by exact evidence** (the tree's head commit against `origin/master` after a fetch, and against the 243 pull requests):

| Evidence for the tree's exact head | Trees (97 linked) | Size |
|---|---:|---:|
| Head is already part of `master` | 67 | 2.19 GB |
| Head is in an open pull request (9) or a closed, unmerged one (7) | 16 | 0.53 GB |
| Head is on a branch, no pull request | 12 | 0.33 GB |
| Head is on no branch, tag or remote ref | 2 | 0.02 GB |
| A merged pull request whose head equals the tree's head, but the head is not part of `master` (the squash case) | 0 | 0 |

The squash case is rare here today, but it is real elsewhere, and integration branches do happen (#386 and #387 reached `master` through #393). The rule below handles both.

What the numbers say about the rule:

- Of the 67 merged trees, none has ignored content beyond regenerable caches, and none is dirty. 20 have no ignored files at all.
- **No tree has a "finished" note today**, because the note does not exist yet. A rule that needs the note would reclaim nothing until every agent adopted it, which is the forgetting the operator describes.
- None of the 98 is a lane worktree. The lane folder (`.worktrees/`) is empty: lane delivery removes its own worktree. All the sprawl is worktrees that agents made by hand, in their scratch folders.

### 1.2 What else takes the space

The scratch area (`atk-scratch`) is **11.27 GB** by the sizes we could read. The registered worktrees inside it are 2.98 GB of that (91 trees). The rest is not worktrees:

| What | Size | Entries |
|---|---:|---:|
| Task folders that contain registered worktrees (the worktrees plus other files beside them) | 3.27 GB | 26 |
| Folders with no worktree marker: plain 3.28 GB, test and cache directories 1.30 GB, folders with a top-level `.git` entry 0.36 GB, one virtual environment 0.03 GB | 4.96 GB | 379 |
| Folders that contain another clone or an unregistered worktree (the janitor refuses these) | 1.53 GB | 47 |
| Loose files at the top of seat folders | 0.06 GB | 3,823 |
| Folders the account cannot list (access denied): **not measured** | unknown | 137 |
| `node_modules` at the top of a folder | none found | 0 |

Repository root (read-only look): `.tmp` 1.61 GB, `dist` 0.53 GB, the live `.agenttalk` store 0.22 GB (never a target), `.git` 0.13 GB.

The janitor report mode (run once, 2 minutes) lists 208 candidates and 98 registered worktrees. It could not list 210 folders: 162 "access denied" and 48 that contain another `.git` entry. Several of those are exactly the large test-directory families that matter.

### 1.3 What this means

1. **A worktree sweep on its own cannot remove "dozens of GB".** All the linked worktrees on this desktop are 3.07 GB, and 2.19 GB of that is merged. Under today's janitor rules 1.81 GB is removable right now, and the merge-based rule below finds about the same (2.19 GB, or 1.86 GB when the 3-day quiet period is kept). The value of the new rule is safety and exactness, not volume.
2. The larger part of the disk use is **test and scratch directories that are not worktrees**, and a large unknown part sits in folders the janitor cannot read. That is a different problem and needs its own measurement: it is not decided here, and it is not a second cleaner. See section 8, "Goes back to a challenge".
3. The reported total (section 6) must therefore say what it covers, so it is never read as "all the disk we could free".

## 2. What the design is, in one page

- **One deleter.** A central sweep, part of the existing janitor (`src/agenttalk/janitor.py`). There is no second general cleaner. Agents do not delete.
- **Lane delivery keeps removing its own worktree**, as today (it records a pending cleanup and removes the checkout with `git worktree remove`, without force). The sweep retries lane trees that are left in `cleanup_pending` or `cleanup_failed` by calling the same safety check, not by a second implementation.
- **A "finished" note** (a small file in the project store, never inside the tree) that an agent leaves at the end of its work. It deletes nothing and can be left by anyone.
- **"Finished" is decided by merge evidence, not by the note.** The note only shortens the waiting time and makes a tree that will never merge eligible for a person to remove.
- **Keep and report, never delete:** unsaved changes, untracked files, ignored files that are not known caches, unknown or unregistered trees, other teams' trees, trees in use, the main checkout, anything it is unsure about.
- **Remove the checkout only.** Never the branch, never a commit, never a WIP commit made just so a delete becomes possible.
- **Order of introduction:** a number in `status` and `doctor`; then one human command for two weeks; then a timer for one team, only with that team's written authorisation. Nothing unattended before the folder-link fix (#399, pull request #410) is merged and reviewed.

## 3. What counts as finished

Two candidates were compared against the inventory.

**(a) The explicit note plus the existing checks.** The agent says "done", and the sweep checks clean, head reachable, old enough. Result on this desktop today: 0 trees qualify, because 0 notes exist. It also trusts a claim: an agent that is wrong about "done" gets a tree removed that still held a plan. It does not need merge evidence.

**(b) Merge evidence bound to the exact head and the destination.** The tree's head commit is `H`; the destination is the branch the work was meant for (the default branch, or the pull request's base). The tree is finished when one of these holds, checked fresh:
1. `H` is an ancestor of the destination's tip, after a fetch no older than one hour (covers merge commits and integration branches that were later merged).
2. A pull request in state MERGED has its head commit equal to `H` and its base equal to the destination (covers squash merges, where the head is not an ancestor).
3. For work merged into an integration branch first: rule 1 or 2 holds against that branch, and that branch's own pull request into the default branch also satisfies rule 1 or 2. (Chains longer than one step are not recognised: they are reported, not removed.)

Result on this desktop today: 67 of 97 linked trees (2.19 GB) qualify with no note at all.

**Recommendation: (b) decides, (a) adds to it.**
- (b) is the only rule that works without anyone remembering anything, and it checks a fact instead of trusting a claim.
- The note (a) does two small jobs: it shortens the quiet period (an agent that says "done" an hour ago needs no 3-day wait), and it is the only way a tree whose work will **never** merge (the 16 in open or closed pull requests, the 12 with no pull request) becomes eligible. Those are never removed unattended: a person runs the command with the note present.
- A tree is never removed because it is old. Age is a waiting period on top of evidence, not a reason.

Anything beyond this (for example "the pull request was closed, so the work was abandoned", or removing trees whose branch is gone) is not part of this design and goes back to a challenge before it is built.

## 4. What the sweep keeps and what it removes

A tree is removed only when **all** of these hold, checked in this order, and the sweep stops at the first failure and records why:

1. It is a linked worktree registered in this repository, in a folder the project has declared for sweeping, and it is not the main checkout, locked (`git worktree lock`), or marked prunable by git.
2. Its owner folder is the team's (the declared root), not another team's or an unknown one.
3. It is clean: no changes to tracked files, **no untracked files**.
4. Its ignored content is only known regenerable caches (an allow-list: `__pycache__`, `.ruff_cache`, `.pytest_cache`, `.mypy_cache`). Anything else ignored, a `.env`, a local database, test evidence, a nested `.agenttalk` folder, anything under a name we do not recognise, keeps the tree. On this desktop: 95 of the 97 linked trees hold only caches or nothing; none of the 67 merged ones holds anything else.
5. It contains no link (symbolic link or junction) anywhere inside; if it does, it is kept and reported.
6. Its head `H` is finished by the evidence in section 3, and `H` is also reachable from at least one branch, tag or remote ref that is **not** the tree itself, so removing the folder cannot make a commit unreachable.
7. It is not in use (section 5).
8. It has been quiet for the waiting period (default 3 days; 1 hour when a note exists whose head still equals `H`).
9. Nothing about the path changed since the plan was made (section 4.2).

Then, and only then, the sweep runs `git worktree remove -- <path>` **without** force, and checks afterwards that the branch and `H` are still there, that nothing outside the tree changed, and that git no longer lists it. Git itself refuses a dirty tree, so this is a second line, not the first.

### 4.1 What is never done

- No `--force`, no WIP commit, no `git stash`, no deleting a branch, no deleting a commit, no `gc`, no touching the main checkout, the live `.agenttalk` store, other projects, other teams' folders or any folder outside the declared roots.
- No treating "unknown" as "merged". If a fetch fails, the pull request lookup is unavailable, git cannot be run, or a folder cannot be listed, the answer is "keep, and say why".
- No removal of a tree that is only partly readable (for example a folder the account cannot list): it counts as "not measured" in the total.

### 4.2 The plan is checked again at delete time (#399)

The sweep first writes a plan (a list of trees with, for each: the canonical path, the identity of every folder from a trusted anchor down to the tree, the head, the branch, the evidence). Just before each removal it re-reads all of that. A folder above the root that became a link, a changed identity (volume and file ID on Windows, device and inode elsewhere), a changed head, or a new file in the tree cancels that removal and reports it. This reuses the ancestor check being added by #410; it must be merged first.

## 5. How the sweep knows nothing is using a tree

No portable way finds every process that has a folder as its working directory, so the answer is layered, and any uncertainty means "in use".

1. **Agenttalk's own records.** A lane in a state other than delivered, abandoned or cleanup (`lanes.json`) names the tree; a launch request that is not finished (`list_launch_requests`) names the lane or a path inside the tree; an active entry in `supervisor-state.json` (agents and ephemeral reviewers) has a working directory **inside** the tree (the lane check today compares equal paths; the sweep compares "equal or below"). Any wrapper record that names a child's working directory counts the same way, where such a record exists.
2. **Recent writes.** The newest modification time anywhere in the tree and in its git admin folder (`.git/worktrees/<name>`) is older than the waiting period, and nothing has a lock file (`index.lock`, `HEAD.lock`) there.
3. **Open files, best effort.** On Windows, ask the Restart Manager for processes holding files in the tree. On Linux and macOS, `lsof +D` where available. If the probe is unavailable or fails, that is "unsure", not "free". This catches an editor or a test run holding a file, but not a plain shell that only sits in the folder.
4. **The removal itself is the final lock test.** Without force, git removal fails on Windows when any file in the tree is open. If it fails part way, the tree was already verified clean and merged, so nothing unsaved is lost; the next sweep sees a partly removed tree, reports it, and does not repeat blindly.
5. **The note helps here too:** an agent that leaves "finished" is saying it has stopped using the tree. A shell left open in a folder by a person is the case no check covers; for that reason the first two weeks are a human command, not a timer.

## 6. Rollout

1. **Report only.** `agenttalk status` and `agenttalk doctor` show a line such as "worktrees: 54 removable (1.81 GB) of 97, 43 kept, 0 unsure; covers linked worktrees only, not scratch folders or folders that could not be read (137)". The report names the reason each kept tree was kept. It only reads. Janitor `--apply` is untouched and stays suspended.
2. **One human command** (`agenttalk worktree sweep`, dry run by default; `--apply` removes). Used by a person for **two weeks with zero surprises**. A surprise is any of: a removal nobody expected, a kept tree that should have been removed for a reason the report did not give, a kill signal (section 9), a removal that left something half done.
3. **A timer, per team, only with that team's written authorisation** recorded in the project configuration: what (which classes of tree), where (which folder) and how often. The sweep refuses to run unattended in a folder without such an entry. Nothing unattended before #399 is merged and reviewed.
4. **Native Windows first.** Linux and macOS use the same rules; where the platform differs (identity, link kinds, open-file probe, case-insensitive folders on macOS) the sweep refuses instead of guessing, and tests run on all three in CI.

## 7. The tests that must exist

Every case below must leave the tree, the branch and all commits untouched and must say why in the report. They run against throwaway stores and repositories only.

- Refuses: a tracked file modified; an untracked file; ignored-only content that is not a cache; ignored content that **is** a nested `.agenttalk` folder (with a store inside); a `.env`; a head that changed since the plan or since the note; a head that is not merged; a head on no ref; a merged pull request whose head differs from the tree's; a stale or failed fetch; the main checkout; a locked or prunable worktree; a tree outside the declared folder; another team's tree; an unlistable folder inside the tree.
- Refuses: an active writer (a process holding a file open; a recent write; a lock file; a launch request or active agent with its working directory inside the tree; a lane that is not finished).
- Refuses: a **parent folder swapped for a link** after the plan was made (#399), a **link inside the tree**, a tree reached through a link, and a changed folder identity.
- Removes: a clean merged tree with caches only, leaving the branch, the commit and a sibling tree untouched (checked by comparing the parent folder before and after); a squash-merged tree with a matching pull request head; the same tree with a note and a short quiet period.
- Report mode makes no writes (a listing and file-time snapshot of the tree is identical before and after).
- Two sweeps at once: the second refuses (a lock in the store).
- A failure part way (a locked file): reported, no second attempt in the same run, no crash.
- The kill switch (section 9) really stops every entry point, including the timer.
- Windows, Linux and macOS: link kinds, path length, case-insensitive names, read-only files, permission-denied folders.

## 8. Goes back to a challenge before it is built

- The list of known regenerable cache names (section 4, rule 4), and whether any other ignored name may ever be allowed.
- Anything that makes a tree eligible with weaker evidence than section 3.
- The scratch folders that are not worktrees (test directories, measurement folders, copies of repositories, the unreadable ones). The inventory suggests they are most of the disk use. Whether and how they are cleaned is a separate question with a separate measurement, and it must not become a second cleaner.
- Any unattended mode for a class of tree other than "merged, clean, caches only, quiet".

### What was cut, and the smallest correct version

- **Cut:** a trash or restore feature; a WIP commit to make a dirty tree removable; force; a notification system; automatic notes written by the wrapper; a daemon. None is needed for correctness; each adds a way to lose work.
- **Not cut, and why:** the exact-head merge evidence, the fresh fetch, the "in use" layers, the re-check before each removal and the kill switch. They are what keep it safe.
- **Smallest correct version:** a read-only report plus one human command that applies rules 1 to 9 to merged trees. Everything else follows only if the first two weeks pass.

## 9. Kill signals

Any one of these switches the sweep off **everywhere** (a flag in each store that every entry point checks, set by the sweep itself when its post-removal check fails, and shown by `doctor`; the shipped default for any timer is off):

- a removal outside the declared folder;
- a removal after which a commit is no longer reachable;
- a removal that deleted uncommitted, untracked or ignored files;
- anything under a `.agenttalk` store removed.

These mean stop and do not continue the rollout:

- space reclaimed only by forcing, or by treating unknown as merged;
- the reported total does not fall within two weeks of the human command being available: stop adding to the sweep. (The inventory already says why this may happen: worktrees are about 3 GB of a much larger scratch area.)

## 10. Honest estimate

A 4 to 6 day guess covers writing the happy path. The work that guess leaves out:

| Part | Engineer-days |
|---|---:|
| The note command and its small file format; the status line | 1.5 |
| Evidence engine: fresh fetch, ancestor test, pull request lookup by exact head, offline behaviour | 2.5 |
| The nine checks, the plan, the identity re-check, the post-removal verification | 3.5 |
| "In use" layers including the Windows open-file probe and the equal-or-below working directory check | 2.5 |
| `status` and `doctor` totals; the human command and its report | 2 |
| The tests in section 7 on three operating systems (junction, link and lock tests are the slow ones) | 4 |
| Docs, CHANGELOG, skill and manual updates | 1 |
| Review rounds: this project's reviews of deletion code have taken 3 to 5 fix rounds | 4 |
| **Total** | **about 21, give or take half** (11 to 32) |

Calendar time is longer than the work: the two-week human period, then a per-team authorisation, then a decision. Dependencies: #410 (parent-folder check) must be merged before anything unattended. The estimate does not include cleaning the scratch folders that are not worktrees.

## 11. Technical details

**Facts about the code this builds on (master `7e36cfb7`):**
- `janitor.py` reports scratch candidates at the second level under the scratch root (age by the newest modification time in the tree, `keep_days` 3), the entries of `.worktrees/`, repository-root name families, and temporary-folder families. It refuses a task folder that contains a `.git` entry not registered to this repository or that it cannot list. `--apply` today WIP-commits dirty registered worktrees on their own branch, deletes, and prunes; this design retires the WIP commit from any unattended path. It skips `.claude`, `docs`, `tools`, `.agenttalk` and names starting `launch-`, `MANUAL-` or `.wt-` by name.
- `cli.py` lane code: `_lane_worktree_remove_safe` (lane state must be delivered, abandoned, cleanup-pending or cleanup-failed; no active launch; clean and verifiable), `_lane_worktree_idle` (launch requests and supervisor state, equal-path comparison), and lane delivery's `git worktree remove -- <path>` without force.
- Lane records already hold `worktree_path`, `worktree_state`, canonical top-level and common git directory.

**Note format (proposal).** `agenttalk worktree done [--task <request id>] [PATH]` writes `<store>/state/worktree-finished/<tree-id>.json` with: schema version, canonical path, canonical common git directory, head at the time, branch, seat, task request id, time. `tree-id` is a hash of the canonical path and the common git directory. The file lives in the store, not in the tree, so the sweep never reads tree contents to find it and the note outlives the tree for audit. It is valid only while the tree's head still equals the recorded head.

**Sweep sketch.**
1. Read `git worktree list --porcelain` from the repository; ignore the first entry (the main checkout).
2. Fetch the destination refs; record the fetch time. Look up pull requests by exact head only for trees that need it.
3. For each tree, run the checks of section 4 in order and record `kept(reason)` or `removable(evidence)`.
4. Write the plan. For each removable tree, just before removal, redo the identity, head and cleanliness checks; remove with `git worktree remove -- <path>`; verify; append to a sweep log in the store.
5. On any verification failure: set the kill flag and stop.

**Windows.** Detect links by reparse-point attribute, not only symbolic-link flag; do not follow junctions while sizing or scanning; identity from volume serial and file ID; long paths (`\\?\`); read-only attributes under `.git`; antivirus and indexers can hold files briefly, so a failed removal is retried by the next sweep and never forced; folders created under restricted permissions cannot be listed and are "not measured".

**Inventory method (reproducible).** Registered worktrees from `git worktree list --porcelain`; size from a non-following walk; status from `git status --porcelain=v1 --ignored=matching` with optional locks off; merge evidence from `for-each-ref --contains`, `merge-base --is-ancestor` against `origin/master` after a fetch, and the list of all pull requests with their head commit; folder categories from the janitor's own refusal lists. The scripts were run from the author's scratch folder and are not part of this change.

## 12. Not in this change

This pull request adds this document only. It builds nothing and changes no behaviour. Related: #399 (parent folder swap; fix in #410), #342 and #386 (janitor safety), #148 (janitor).
