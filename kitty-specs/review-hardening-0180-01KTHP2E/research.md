# Research & Decision Records — Review Hardening (0.18.0)

All decisions resolve against issue #21, the Codex-accepted proposal
(`pp-47eae0ce`), and the verified current code (see plan.md "Verified code
facts").

## D1 — Signature type guard placement (FR-001)

**Decision**: In `signing.verify_message`, immediately before
`compare_digest`, add `if not isinstance(claimed, str): raise ValueError(...)`.
Keep it inside `verify_message` (the single choke point) rather than
broadening every caller's `except`.

**Rationale**: `verify_message` already raises `ValueError` for missing /
wrong-version / mismatched signatures; a non-string value is just another
"this signature is invalid" case and belongs in the same vocabulary. Every
read-path gate already catches `ValueError`, so one guard makes the poison
file a normal invalid (quarantinable) message everywhere at once — including
`list_invalid_messages` itself, which is what makes it pruneable.

**Alternatives**: broaden gate catches to `(ValueError, TypeError)` — rejected:
shotgun, and a `TypeError` elsewhere would then be silently swallowed.
Validate signature type in `Message.from_raw` — rejected: signature presence
is only meaningful when signing is enforced; the verify path is the right
layer.

## D2 — id-shape validation (FR-003, C-008)

**Decision**: Add a module-level `_ID_RE` in `store.py` **built from the same
constants `_new_id` uses** — `re.compile(r"\A\d{8}-\d{6}-\d{6}-[" +
re.escape(_ID_ALPHABET) + r"]{4}\Z")`. `Message.from_raw` rejects an id that
doesn't match (raising the same `ValueError` shape as its other schema
checks), so the file is classified invalid at scan time and is quarantinable.

**Rationale**: from_raw is the canonical scan gate feeding
`list_invalid_messages`/quarantine; validating there keeps the "malformed →
quarantinable" contract uniform and stops a malformed id before it can deliver
or move a cursor. Deriving the regex from `_ID_ALPHABET` means the validator
and the generator cannot drift.

**Scope honesty (C-008)**: this rejects ids of the wrong *shape* only. Clock
skew across synced machines yields **well-formed future-dated** ids that match
`_ID_RE` — D2 does NOT catch them and MUST NOT be claimed to. That ordering
hazard stays a documented constraint (D8 / WP04 docs).

**Back-compat**: every real id is `_new_id`-generated and matches; the regex
is tested against a large freshly-generated batch incl. the monotonic `+1µs`
bump near second/minute rollovers. Malformed ids were never
deliverable-by-design, so reclassifying them is additive, not breaking.

## D3 — Liveness mechanism (FR-007, FR-008, C-001, C-005)

**Decision**: A stdlib, fail-quiet `_process_alive(pid) -> bool` in
`store.py`:
- POSIX: `os.kill(pid, 0)` → True; `ProcessLookupError` → False;
  `PermissionError` → True (exists, not ours); any other OSError → False.
- Windows: `ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)`
  (`PROCESS_QUERY_LIMITED_INFORMATION`); non-null handle → alive (then
  `GetExitCodeProcess`/`STILL_ACTIVE` check, `CloseHandle`); null → not alive.
  Any ctypes error → fall to "can't prove alive" → False.
- Never raises. `pid <= 0` or non-int → False.

**Rationale**: Codex preferred `OpenProcess` over a `tasklist` shell-out —
stdlib-pure, no subprocess, no PATH dependency. POSIX `os.kill(pid,0)` is the
canonical liveness probe. Failing to the False ("not alive") side means an
uncertain probe simply suppresses the advisory warning rather than firing a
false one or crashing.

## D4 — Duplicate-detection semantics (FR-007, FR-008, FR-009)

**Decision**: `Store.foreign_wait_pid(agent, self_pid, *, now=None,
stale_after=None) -> int | None`: read the existing `.waiting` marker; if it
exists, is fresh, carries a `pid` that is `!= self_pid` and
`_process_alive(pid)` → return that pid; else None. **`store.py` must NOT
import CLI staleness constants** (Codex note): the freshness threshold + clock
are passed IN as parameters (the `cli.py` caller supplies its existing
`STALE_THRESHOLD_SECONDS` / `time.time()`), or default to a store-local
constant — store stays self-contained and unit-testable. A starting
`agenttalk wait` (in `cli.py`) calls this BEFORE writing its own marker and,
on a non-None result, prints one advisory stderr line. `listen` is a skill
that calls `wait`, so it inherits the warning; there is no `agenttalk listen`
CLI command. Never blocks, never changes the exit code.

**Framing (Codex constraint)**: a single per-agent marker cannot be a complete
duplicate registry (a second writer overwrites the first), so the feature
detects "another live process currently holds the marker", not "all
duplicates". Docs and the warning say exactly that.

**Why before-overwrite**: `write_waiting` overwrites unconditionally; reading
first is the only point where the prior owner is still visible.

## D5 — `broadcast --resume` skip-retired (FR-005, C-004)

**Decision**: In the resume path, partition the still-missing frozen
recipients into active vs retired (via the current roster /
`retired_agents`). Send only the active ones. Report `dropped=[retired...]`
on stderr alongside the existing `delivered`/`missed` manifest. Exit codes:
- some active copies still failed → exit 5 (unchanged partial-failure
  semantics) with dropped noted;
- all active copies sent, only retired remained → **exit 0** (resolved), with
  dropped noted;
- all remaining were retired (nothing to send) → **exit 0**, dropped noted.

**Rationale**: a tombstone can never receive, so a retired frozen recipient is
permanently undeliverable-by-design — counting it as an open "missed" traps
the broadcaster at exit 5 forever (the bug). Dropping-and-reporting is the only
path that lets the batch reach a terminal resolved state. C-004 is *narrowed*
(fewer exit-5 cases), never broadened.

## D6 — `audience_retired` shape (FR-006)

**Decision**: `threads._derive_broadcast` computes `pending` against the
audience **minus the current retired set**, and `_derive_next` never lists a
retired member as `await-reply`/`next_owner`. The frozen `audience` stays
unchanged (immutable history). Add an additive `audience_retired: [names]` to
the broadcast thread dict — **absent when empty** (additive-not-null), emitted
in `to_dict()` only when non-empty, mirroring how `responded`/`pending` are
broadcast-only.

**Rationale**: observability — the operator can still see a tombstone was once
in the audience, without it generating a perpetual obligation. Absent-when-empty
keeps pairwise/clean-broadcast output byte-identical (the 0.15.0 additivity
gates stay green).

**Additivity gate note**: like the 0.16.0 `next_*` split, `audience_retired`
appears only on broadcast threads that have retired members; the
`_OPEN_THREAD_KEYS`/coordination gates may need it added — handle in WP02 if a
gate trips.

## D7 — Roster parity scope (FR-004)

**Decision**: switch `web._all_messages` (web.py) and the `tail` command
(cli.py) from `cfg.get("agents")` to the known roster (active ∪ retired) — the
same `_known_roster(cfg)` that `_validated_messages` uses. Nothing else about
those code paths changes.

**Rationale**: restores the D3 (#19) invariant uniformly — retired identities'
history stays valid and visible everywhere, and the dashboard's two panels
stop disagreeing. This is the fix that makes the render set match
`valid_messages`. Visible behavior change (more messages shown), so CHANGELOG
notes it; existing shape tests stay green.

## D8 — Scope-honesty documentation (C-006, C-008)

**Decision**: WP04 adds two short, honest doc notes:
- README + SECURITY: **same-agent concurrent consumption is unsupported** —
  one window per agent; 0.18.0 warns (best-effort, FR-007) but does not
  enforce; `advance_cursor`/`mark_thread_seen`/`close_thread` are atomic
  writes, not process-safe read-modify-write.
- README + SECURITY: **cross-machine clock agreement** — the id-cursor order
  is lexical over timestamp-prefixed ids; a synced store across machines with
  skewed clocks can misorder/hide messages; id-shape validation (FR-003) does
  NOT fix this.

**Rationale**: both are real limitations the reviews surfaced; documenting
them is the honest deliverable (the 0.16.0 SECURITY-honesty discipline). We do
not overclaim that 0.18.0 closes concurrency or skew.
