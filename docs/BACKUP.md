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

## Where backups live

`%LOCALAPPDATA%\agenttalk\recovery\<project_id>\<timestamp>\` on Windows,
`$XDG_CONFIG_HOME/agenttalk/recovery/<project_id>/<timestamp>/` (default
`~/.config`) on POSIX - the same per-user base directory `hmac-init` uses
for signing keys, overridable via `AGENTTALK_RECOVERY_DIR` for tests or an
operator who wants recovery storage on a different volume.

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

- **The snapshot is a real point-in-time view, not a torn read.** Writers
  are briefly fenced (the same lock `send()` uses) around a hardlink clone
  of the store tree - not a copy while things keep changing underneath it.
  Every file this codebase writes to `.agenttalk/` uses a write-temp-then-
  `os.replace` pattern, which never mutates the inode a hardlink already
  points to - so once cloned, a file is frozen even after the fence is
  released and live writes resume.
- **Every file's hash in the manifest matches its actual snapshot bytes.**
  Verified twice: once when the manifest is written, once more as a
  self-check before the backup is published. A hash mismatch raises and
  the backup is discarded (see "Failure behavior" below) rather than
  silently shipping a manifest that lies about its own contents.
- **A killed or crashed backup never leaves something that looks
  complete.** The clone is staged under a temp name and only renamed into
  its final timestamped directory after the manifest is written and
  self-verified.
- **Concurrent `send()` calls do not queue up behind the backup.** The
  fence covers only the hardlink clone step (bounded by file count -
  directory-entry creation, not the aggregate size of the store), never
  the hashing or manifest-write. This is a deliberate design choice made
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
  guard file's hardlink count isn't exactly 1). A restored store creates
  fresh lock files on first use; it never needs the old ones.
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

## Failure behavior

`agenttalk backup` either succeeds completely (prints a destination,
exits 0) or leaves nothing published (exits 2, prints the error, any
partial state is a discarded `.tmp-*` staging directory only). There is
no partial-success mode a script needs to detect.
