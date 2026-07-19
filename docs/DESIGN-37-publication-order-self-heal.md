# DESIGN — #37 publication-order self-heal + writer-skew diagnostics

**Status:** design, for adversarial review before build
**Author:** `claude-agenttalk-lead` · 2026-07-19
**Severity:** elevated — this failure muted a 5-agent team for ~80 min (second
laptop) AND muted this team this session (the enforcement merge→revert→re-merge
on a live bus). GH #41.

## 1. The failure, precisely

The store keeps a **publication-order sidecar** — `message-publication-order.json`
(map `message_id → sequence`, `append_sequence = N`) plus a
`.anchor.json` (`append_sequence`, `chain_digest` over sequences `1..N`). It
durably pins the physical append order so history cannot be silently reordered.

`_reserve_message_publication_sequence` (`store.py:1602`, WRITE path, under the
publication lock) and `publication_ordered_messages` (`:3753`, READ path) both do
the same check: every on-disk validated message must have an entry in the
sidecar. If any is **missing**, both raise:

> `validated message is missing durable publication order: <id>`

**How the missing state arises — version skew, one-way and undiagnosable:**
a store is shared by two writers reporting the *same* `--version` (e.g. a
`PYTHONPATH=src` build vs an installed `0.78.x` wheel). One writer has
publication-order support, the other does not. The order-less writer appends
`messages/<id>.json` files **without** touching the sidecar. Now the disk has
messages `N+1..M` that the sidecar (frozen at `N`) never recorded. The next
send by the order-aware writer sees `missing = [N+1, …]` and raises — **for every
agent, on every outbound**, including the escalation that would report the
outage. Whole-team mute. `--version` cannot discriminate the two writers, so the
cause is invisible.

Manual recovery used twice now: move both sidecar files aside → the store
re-bootstraps from id-order on the next send. Effective, but requires a human who
knows the trick, after a total-comms outage.

## 2. Three fixes

### Fix 1 — self-heal the skew case; keep failing loud on tampering

**Key insight (the safety argument):** `_read_message_publication_order` verifies
the anchor covers the *entire* sidecar — the anchor is written with
`append_sequence == N` and a `chain_digest` over `1..N` every time (`:1636`,
`:1648`). So if `_read` returns an order at all, the prefix `1..N` is
cryptographically pinned and intact. The "missing" messages are **new appends
beyond the pinned prefix**, not modifications to it.

Therefore healing by **appending the orphans at the tail in deterministic
id-order** — the *same rule the initial bootstrap already uses* (`:1610`
`sorted(existing_messages, key=id)`) — never touches the pinned prefix. It cannot
reorder or remove anchored history; it only assigns tail sequences `N+1, N+2, …`
to messages that are already validly on disk. An attacker who writes a message
file directly still only lands it at the tail — exactly where a normal `send`
would have put it. **Auto-heal-at-tail adds no attack surface.**

- **WRITE path** (`_reserve…`, under the publication lock): on `missing`, fold
  the orphans into the order at the tail (id-sorted), persist the extended
  sidecar + re-anchor, emit a **WARNING** naming the cause, then reserve the new
  message's sequence as normal. No raise; the send succeeds.
- **READ path** (`publication_ordered_messages`, no lock, must not write): on
  `missing`, fold the orphans at the tail **in memory** and return the ordered
  list. Deterministic and identical to what the next write will persist, so a
  read before the heal-persist and a read after agree. Never wedges a read.
- Shared helper `_extend_order_with_orphans(order, orphan_ids)` used by both so
  the fold rule lives once.

**Still fails loud (these are genuine corruption/tampering, not skew), now with
cause-naming messages (Fix 3):**
- anchor present but sidecar absent (`:1575`),
- sidecar rolled back below anchor (`:1593`),
- chain digest mismatch below anchor (`:1599`) — the tamper detector,
- unreadable / schema-invalid / non-contiguous sidecar (`:1582`, `:1536`, `:1553`).

**Out of scope (documented, not defended):** an attacker with filesystem write
access to `state_dir` who deletes *both* sidecar files forces a legacy-style
bootstrap. That also destroys the integrity anchor; nothing short of external
attestation defends it, and it is not the version-skew threat this addresses.

### Fix 2 — make the running code discriminable

Two writers reporting `0.78.0` must be tellable apart. Add to **`doctor`** (and
`status` where cheap) a build/capability fingerprint:

- `agenttalk_module_path` = the package directory actually imported
  (`Path(agenttalk.__file__).parent`) — a `src/` checkout vs a site-packages
  wheel differ here even at equal `--version`.
- `store_schema_capabilities` = a static list of capability tokens the running
  code supports, e.g. `["message-publication-order/v1"]`. A writer lacking the
  token is the one that can corrupt the sidecar invariant.

This is diagnostic surface, not a gate; it lets a human (or `doctor`) see the
skew that `--version` hides. (A stronger future step — stamping the store with
`last_writer_capabilities` on every write so a reader auto-detects a
capability-deficient writer — is noted but deferred; it touches every write.)

### Fix 3 — error messages name the cause, not the symptom

The remaining fail-loud raises currently state the symptom. Reword to name the
cause and the action, e.g.:

> `publication-order sidecar changed below its anchor (chain digest mismatch) —
> the durable order was modified or corrupted; this is not a recoverable
> version-skew. Investigate before writing; do not delete the sidecar blindly.`

and for the (now-healed) skew path the WARNING reads:

> `healed <k> message(s) that were written without a publication-order entry — a
> writer without message-publication-order/v1 support (e.g. an older agenttalk
> install sharing this store) appended to it. Upgrade all writers to stop the
> skew.`

## 3. Test plan

- **Skew heal, write:** seed a sidecar at N, drop `k` extra valid message files
  on disk, call the send path → sidecar extended to N+k+1 (orphans id-sorted at
  tail, then the new message), warning emitted, prefix `1..N` byte-identical.
- **Skew heal, read:** same seed, `publication_ordered_messages` returns all
  messages in `1..N` order then orphans id-sorted, **without** writing the
  sidecar (assert file mtime/content unchanged).
- **Read/write agree:** the in-memory read order equals the persisted order after
  a subsequent write.
- **Tamper still fails loud:** flip a byte below the anchor → chain-digest raise
  with the new cause message; anchor-without-sidecar → its raise; rolled-back
  sidecar → its raise. Assert heal does NOT fire for any of these.
- **Idempotent:** healing an already-healed store is a no-op (no spurious
  re-anchor, no duplicate sequences).
- **Contiguity preserved:** after heal, `sequences == set(range(1, M+1))` so
  `_validate_message_publication_order` accepts the result.
- **Fix 2:** `doctor` reports a module path distinguishing a `src` import from an
  installed one, and lists the capability token.
- Cross-platform (the enforcement lesson): run POSIX + Windows.

## 4. Review asks

1. Attack the safety argument in Fix 1: is there ANY sequence of writes/crashes
   where auto-heal-at-tail alters or masks a change to the pinned prefix `1..N`,
   or where the in-memory read fold and the persisted write fold diverge?
2. Should the write-path heal be **silent+warn** (proposed) or require an
   explicit `doctor --heal` opt-in? The incident argues for automatic (the
   outage was total and the fix is provably prefix-preserving); the counter is
   "never self-modify integrity state without a human." I lean automatic *because*
   the prefix is provably untouched — challenge that.
3. Is id-order the right tail rule for orphans, or should it be file mtime? (id
   is timestamp-prefixed and is already the bootstrap rule; mtime is spoofable
   and clock-dependent. I prefer id.)

---

## 5. Review round 1 — findings folded (2026-07-19)

An independent adversarial review attacked the safety argument. It **confirmed
the core holds** (no read-fold/write-fold divergence; duplicate sequences fail
loud via contiguity validation; auto-heal-at-tail adds no injection surface *when
signing is enforced*). It found one HIGH and several real refinements, all folded
below. The design is revised accordingly; the merged-file shortcut is explicitly
rejected because the **separate anchor is the tamper-evidence** (a full-file
rewrite would otherwise recompute its own digest and defeat detection).

- **[HIGH] Lock-free read misclassifies a concurrent write as `rolled back`.**
  The read path reads sidecar then anchor unlocked, so it can observe (sidecar@N,
  anchor@N+1) mid-write and raise the tamper error `:1592`. Downstream
  (`obligations.py:1954`, `:7264`) this silently degrades owed-action/terminal
  projection to "unavailable" with a false tamper cause.
  **Fold:** in `_read_message_publication_order`, **read the anchor first, then
  the sidecar.** The sidecar's `append_sequence` is monotonic (append-only, prefix
  never rewritten), so anchor-read-first guarantees `anchor_seq <= sidecar_seq`
  for any race; a bounded single re-read absorbs the torn window. Only a
  **durable** `anchor_seq > sidecar_seq` remains, which now correctly means the
  sidecar lost committed history (genuine corruption) → fail loud. This also
  repairs the pre-existing intermittent false-tamper on every busy send.

- **[MED] Safety premise fails when the anchor is absent.** `_read` returns the
  sidecar *unverified* when the anchor file is missing (`:1584-1585`), so "prefix
  always pinned" is false after a crash during first-bootstrap (sidecar written,
  anchor not — `:1640` before `:1644`) or a deleted anchor.
  **Fold:** correct the claim to "pinned **when the anchor is present**." On
  sidecar-present/anchor-absent: still contiguity-validate the sidecar, serve
  reads, **re-anchor on the next write** (restoring the pin promptly), and have
  `doctor` WARN. Not fail-loud — that would wedge a benign crash-recovery.

- **[MED] Heal WARNING asserts a cause it cannot verify.** Fix 2 admits the
  running code can't discriminate writers, yet the WARNING names version-skew.
  **Fold:** reword to *"healed <k> orphan message(s) with no publication-order
  entry (cause undetermined: version skew or a writer that bypassed
  `_reserve_message_publication_sequence`) — upgrade/inspect writers."* Name skew
  only if a Fix-2 capability fingerprint actually confirms it.

- **[MED] Windows crash between the two writes.** File data is fsynced on both
  platforms (`_atomic.py:68`) but the rename is dir-fsynced only on POSIX
  (`:87`, no-op on Windows), so rename ordering isn't code-forced on Windows.
  **Fold:** rely on NTFS metadata journaling ordering sidecar-rename before
  anchor-rename (writes are already sidecar-then-anchor), and treat a durable
  `anchor > sidecar` as fail-loud corruption (per the HIGH fold) rather than
  auto-heal. **Add a crash-injection test** across the two-write boundary.

- **[LOW] Cross-clock orphan order can invert a request/response** in the
  obligations reducer (a fold over publication order). Inherent to id-order under
  clock skew. **Fold:** state inter-orphan order is best-effort; the reducer must
  already tolerate out-of-causal-order delivery (a distributed bus property), so
  this is a documented limitation, not a heal blocker.

- **[LOW] Heal's own two-write window** leaves the healed tail un-anchored until
  the next write. Bounded (the anchored prefix stays safe; next write re-anchors).
  **Fold:** stated as a known, bounded window.

- **Signing dependency (stated):** injection-safety of auto-heal-at-tail holds
  because a healed file must pass `valid_messages()` (HMAC when
  `signing_enforced()`). With signing OFF, any schema/roster-valid file heals in
  — but such a store had no forgery barrier to begin with. Made explicit.

**Net:** the read path change (anchor-first + bounded re-read) is the substantive
addition beyond the original design; everything else is message/claim tightening
plus the crash-injection test. Ready to build against this revision; the
implementation goes through a build review before merge.
