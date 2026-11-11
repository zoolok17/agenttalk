# `agenttalk backup`: what it guarantees, and what it does not

Audience: anyone relying on `agenttalk backup` to protect a project's bus
store, and anyone building increments 2-4 (restore, vanished-store
detection, archive-out-of-tree) on top of its manifest format. See issue
#156 for the incident and design history.

## The problem this solves

The bus store (`.agenttalk/`) lives entirely inside the project tree. A
routine `rm -rf .agenttalk`, `Remove-Item -Recurse .agenttalk`, or `git
clean -fdx` (which deletes gitignored directories, and `.agenttalk/` is
gitignored by convention) destroys every message, the roster, cursors,
and archived cold storage in one shot, with nothing in agenttalk noticing
or warning first.

`agenttalk backup` writes a verified snapshot of the store to a per-user
location genuinely outside the project tree, so it survives all three of
those.

## Usage

```text
agenttalk backup [--json]
```

Prints the destination directory, the manifest path, and the manifest
hash. `--json` emits a machine-readable equivalent (`project_id`,
`destination`, `manifest_path`, `manifest_hash`, `file_count`,
`total_bytes`, `hardlink_used`, `sequence_at_snapshot`, `fence_seconds`).

The `mode:` line says how the files were made. `hardlink` means every file was linked.
Launch requests and lane delivery records are always copied (see below), so a store that
has any shows `hardlink (2 launch-request and lane-delivery files are always copied)`.
`copy (no hardlink support)` is for a disk that cannot link files, and a file whose link
failed is counted as `copied because a link failed` (if every link failed, the
line says `copy (every link failed: ...)`, not "no hardlink support"). In `--json`, `hardlink_used` is true
only when every file in the snapshot was linked; copying some files on purpose makes it false.

## Where backups live

`%LOCALAPPDATA%\agenttalk\recovery\<project_id>\<timestamp>\` on Windows,
`$XDG_CONFIG_HOME/agenttalk/recovery/<project_id>/<timestamp>/` (default
`~/.config`) on POSIX - the same per-user base directory `hmac-init` uses
for signing keys, overridable via `AGENTTALK_RECOVERY_DIR` for tests or an
operator who wants recovery storage on a different volume.

Under the test-and-probe store fence (`AGENTTALK_STORE_FENCE`), the backup folder must lie
inside the fence, as the store must. Otherwise `agenttalk backup` refuses before it makes
any folder, prints why and exits 2; set `AGENTTALK_RECOVERY_DIR` to a folder inside the
fence instead.

`project_id` is `signing.project_id_for_root`: a SHA-256 of the resolved
project root path, not of anything inside `.agenttalk/`. Deleting and
recreating the store at the same path recomputes the identical id, so a
later backup lands beside earlier ones automatically. Moving the project
to a new path changes the id (documented limitation, same as
`hmac-init`'s key file: the old backups are still on disk, just no longer
found by an unqualified `agenttalk backup` run from the new location).

Each backup gets its own timestamped directory with a self-contained
`manifest.json` - nothing is ever overwritten, and increment 2 (`restore`)
will be able to pick a specific one or default to the latest.

## What it guarantees

- **No file in the snapshot is a torn read, and the messages and their
  publication order are one point in time.** The backup takes the lock
  `send()` uses (the message publication lock) around a hardlink clone of
  the store tree, so messages cannot be published while it runs, and it
  reads the publication order under the same lock. That lock is the only
  thing the backup shares with the writers: other files (cursors, config,
  health and other state, launch requests, lane delivery records) are
  written under their own locks or none, so while the clone runs one of
  them can change, and the snapshot can hold files from slightly different
  moments within that short time. Each file is whole, though: every file
  this codebase writes to `.agenttalk/` uses a write-temp-then-`os.replace`
  pattern, which never mutates the inode a hardlink already points to - so
  once cloned, a file is frozen even after the lock is released and live
  writes resume.
- **Every file's hash in the manifest matches its actual snapshot bytes.**
  Verified twice: once when the manifest is written, once more as a
  self-check before the backup is published. A hash mismatch raises and
  the backup is discarded (see "Failure behavior" below) rather than
  silently shipping a manifest that lies about its own contents.
- **A killed or crashed backup never leaves something that looks
  complete.** The clone is staged under a temp name and only renamed into
  its final timestamped directory after the manifest is written and
  self-verified.
- **A `send()` during a backup waits for the lock, briefly, and gives up
  after 10 seconds.** The backup holds the lock for the clone and for reading
  the publication order (bounded by file count - directory-entry creation,
  not the aggregate size of the store), never for the hashing or the manifest
  write; a send that has to wait gets the lock when the backup lets go, and
  fails with a timeout only if that takes more than 10 seconds. This is a
  deliberate design choice made
  in response to issue #154 (still open): a fence around the WHOLE
  operation, including a full validation/hash pass, would reproduce that
  issue's lock-timeout-under-contention at snapshot scale. A load test
  (`tests/test_recovery.py::test_backup_fence_does_not_cause_send_timeouts_under_concurrent_load`)
  pins this: six threads sending in a tight loop against a 300-message
  store while a backup runs, asserting zero `TimeoutError`s.

## What it does NOT guarantee

- **This is not a replacement for #154's actual fix.** #154 (a
  `TimeoutError` on ordinary `send()` under contention, unrelated to
  backup) is still open. Backup avoids making that specific problem
  *worse*; it does not make it go away.
- **Not exactly-once for in-flight side effects.** A message a child was
  mid-way through acting on when the snapshot was taken has no way to
  record "acted on but not yet marked consumed" more precisely than the
  snapshot's own recorded publication sequence.
- **Three sidecar files are excluded from the atomicity guarantee above**
  (all self-documented as best-effort/advisory in their own module
  comments, none of them core durable state): the dead-letter resolution
  sidecar, the reply-refusal reason sidecar, and the `tail` command's
  own follow-cursor. A backup taken mid-write to one of these three could
  contain a stale or missing copy of just that file; it can never contain
  torn or corrupted message, config, cursor, health, or publication-order
  state.
- **Lock/guard marker files are never included in a backup at all** (not
  hardlinked, not copied) - hardlinking one would let a stale backup
  break every future acquisition of that same lock on the LIVE store
  (this codebase's own generation-guard locks refuse to run if their
  guard file's hardlink count isn't exactly 1). Recognized via
  `store.is_lock_or_guard_artifact` - the lock module's own exported
  naming-convention predicate (every current and future lock follows a
  `.lock`/`.generation`-suffixed name, plus two legacy bare exceptions),
  not a separately hand-maintained list in the backup code, so a new
  lock added to `store.py` is covered automatically rather than needing
  a matching edit here. The same rule leaves out the short-lived private
  names the lock code gives a file while it publishes or removes it (a dot,
  an optional name, 32 hexadecimal characters, then `.prepare` or `.unlink`;
  `store._LOCK_HELPER_NAME`). Such a name can be there when the backup lists
  the folder and gone a moment later; copying it would fail the backup or put
  a stray file in the snapshot. A private `.unlink` leftover that a crash
  kept is left out of the snapshot too and is not deleted. A restored store
  creates fresh lock files on first use; it never needs the old ones.
- **Launch requests and lane delivery records are copied, never linked.**
  These files (`state/launch-requests/<name>` and everything under
  `lane-deliveries/`, the `.prepared` folder included) are written under
  their own locks (a launch request under the config and retirement locks, a
  committed delivery under its lane's transaction lock), not under the
  backup's message publication lock, so a backup can run while one is being
  written. The writer checks right after writing that the file has exactly
  one name. A link made by a backup in that moment would add a second name
  and make the writer fail, so the backup copies them (`store.
  is_copy_only_backup_path`). The folder names are matched without regard to
  case: on Windows and on a case-insensitive Mac volume `STATE/Launch-Requests`
  is the same folder, and on a case-sensitive disk a folder that only differs
  in case is another folder, which is merely copied instead of linked. The
  `archive/` folder of launch requests is ordinary and is linked. Anyone
  adding another file written the same way must add its folder to that rule.
- **This command does not restore, delete-protect, or detect a vanished
  store.** Those are #156 increments 2-4, designed (see the issue) but
  deferred to a later release. Today, `agenttalk backup` only writes the
  snapshot - getting a project back from one is a manual file operation
  until `agenttalk restore` ships.
- **Hardlink mode requires the recovery directory to be on the same
  filesystem/mount as the project** (a hardlink cannot cross that
  boundary). When it isn't - or the filesystem doesn't support hardlinks
  at all - `agenttalk backup` falls back to a byte copy automatically
  (`hardlink_used: false` in `--json` output). Still correct, just
  slower, and the fence duration becomes proportional to store size in
  that mode - the same tradeoff #154 already describes, now bounded to
  only the (much rarer) no-hardlink case instead of every backup.

## When the store changes while it is copied

A file can be moved or removed after the backup listed the folder (a launch
request moving to its `archive/` folder is an example). The backup then throws
that attempt away completely, empties its staging folder and lists the store
again, so a moved file is never left out of the snapshot and nothing stale
from the first attempt stays in. A folder or file that cannot be listed or
read for any other reason, such as "access denied", is not retried: the
backup refuses.

All attempts share one budget that starts inside the lock, before the first
listing, and is never reset: at most 3 attempts, and no new attempt starts
once 3 seconds have passed (throwing an attempt away counts). The budget
decides when a new attempt may start. It does not limit how long the lock is
held, and a file operation that is already running (a slow disk, a large file)
can take longer than the budget; senders waiting for the lock give up after 10
seconds as before. When the attempts run out, `agenttalk backup` prints that
the store kept changing, exits 2, publishes nothing and releases the lock:
run it again.

## Failure behavior

`agenttalk backup` either succeeds completely (prints a destination,
exits 0) or leaves nothing published (exits 2, prints the error, any
partial state is a discarded `.tmp-*` staging directory only). There is
no partial-success mode a script needs to detect.
