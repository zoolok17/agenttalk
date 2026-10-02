# Design: attention panel history (stale supervisor warnings, old failed messages)

## In plain words

The console's "what needs you" panel should show only what really needs a person.
Today it can also show warnings left behind by a supervisor that already stopped, and
old failed messages that pile up without end. An earlier attempt (PR #272) tried to
filter these out on the server; a review found that most of that filtering could hide
a real safety warning, which on this panel is the worst thing that can happen. This is
the contract for doing it safely: a warning may move to a lower-priority "history"
section with its age shown, but it is never silently removed, and whenever the system
is not sure, it shows the warning at full severity instead of guessing it away.

What you will notice once this ships: the panel is exactly as alarming as today for
anything still a real problem. A supervisor provably not running anymore moves its
warnings to a quieter history section instead of sitting at the top forever; old
failed messages that are still unresolved group into one row you can still act on
individually; nothing that could still be real ever disappears.

What you need to do: nothing yet — this document has no code. It is the contract later
implementation PRs must satisfy and be reviewed against.

---

## 1. Supervisor state: running / not running / unknown

Before a supervisor warning can move to history, the system must be SURE the
supervisor is gone. Any doubt keeps it active.

- **running** — a valid marker names a process, confirmed alive and confirmed to be
  the same process that wrote the marker (not a different process reusing its PID).
- **not running** — a valid marker names a process confirmed dead, or confirmed to
  now be a different process (PID reused). Never reached just because the marker is
  old; checked fresh every read, so a restarted supervisor is "running" on the very
  next check.
- **unknown** — an unreadable/invalid marker, or an inconclusive process check (e.g.
  access denied). Fail-safe default; shown at full severity, same as "running".

**Technical detail.** Read `supervisor.instance.lock` with
`Store.read_supervisor_instance_strict()` (`absent`/`valid`/`invalid`) — never
`read_supervisor_instance()`, which collapses `invalid` into the same `None` as
`absent` (a damaged marker from a LIVE supervisor would then read as "not running" —
the review's P2). Both `absent` and `invalid` map to **unknown**; only `valid` is
eligible for "not running". For a `valid` marker, use
`Store._probe_owner_identity(pid, pid_start)`: it checks raw liveness
(`_process_liveness()`), and for an alive PID compares the process's creation time
(`_process_start_token()`) against the marker's recorded one — a different creation
time for the same PID number means the original process is gone. Map its outcomes:
`ALIVE` → running; `DEAD`/`PID_REUSED` → not running; `UNKNOWN`/`START_UNMATCHABLE` →
unknown. This reuses existing, already-tested lease-steal machinery; nothing new.

## 2. History vs. active

Only the supervisor-state check above may move a process-tree hold to history.
Nothing else (age, repeat count) may. Every other item kind (escalations, config/gate
holds, coordination stalls) stays active unconditionally.

`process_tree_hold_items()` keeps returning every current hold, unfiltered, to every
caller, stamped with the new tri-state (`supervisor_state`). **The active/history
split happens only in the `/api/attention`/`/api/risk-register` wire layer, never
inside the projector.** This matters concretely: `agenttalk supervise
--reset-process-tree-ownership` (`cli.py:13710`) calls the projector directly and
requires exactly one matching hold; if the split happened there, a hold correctly
demoted to history would vanish from that list and the command — whose whole job is
clearing holds for stopped supervisors — would wrongly refuse. Keeping the projector
whole means that caller needs zero changes. A history item keeps its full evidence and
severity field; only its queue state and where the console shows it change. A
degraded read (unreadable config/roster) is never evidence of "not running" — it
already fails toward active today and this design does not weaken that.

## 3. Defer must not hide a warning that becomes real again

A disposition only applies while the live item's `source_hash` still matches the
snapshot it was recorded against (`apply_disposition()`, gate 1, already general).
Fix: **fold the active/history state into the content `source_hash` is computed over**
for process-tree holds, before `_mk_item()` hashes it. A defer recorded against the
history-state hash then cannot match once the item recomputes as active (a different
hash), so it resurfaces, unsuppressed. This fixes the review's P1: today the liveness
downgrade is applied *after* the hash is computed, so active and history hash
identically and an old defer silently survives the transition.

## 4. Grouping old failed (dead-letter) messages

**Recommendation: group, reusing the existing `dedupe_key` display-grouping in
`build_queue()` — not a new mechanism.** `build_queue()` already applies dispositions
per item, filters by state, then groups the visible items by `dedupe_key` into one
representative plus a `duplicates` list; `compute_stats()` counts every item BEFORE
that grouping. Three of four requirements come free, provided each dead letter keeps
its own unique `item_id`:
- **Resolving every member clears the group** — free: once no member's state is in
  the visible set, none reach grouping.
- **The group keeps every member's id** — `duplicates` already carries every member's
  `item_id`/`source_refs`; the gap is the web wire only serializes these for
  escalations today (the review's P2) — must forward them for grouped rows too.
- **Raw statistics keep counting each message** — already true (`compute_stats` runs
  pre-group).
- **The group shows every owner, not just the first** — the one real gap:
  `_attention_agent()` reads only the representative's own refs. The web wire layer
  must compute an explicit, de-duplicated owners list from representative +
  duplicates (fixes the review's other P2).

**What actually changes:** nothing in `build_queue()`. Only `dead_letter_items()`
changes: an entry older than the threshold (§7) gets a *shared* `dedupe_key` (e.g. one
per agent) instead of a unique one — its `item_id` is untouched. **Invariant that must
never break:** `doctor.py:1167`'s `_drop_resolved_dead_letters()` parses a dead-letter
`item_id` as `dead_letter:<agent>:<message_id>` to match resolutions — a shared group
identity must replace only `dedupe_key`, never `item_id`. A plain capped list (no
grouping) was considered and rejected: it does worse on all four requirements for no
less code, once `dedupe_key` is reused rather than reinvented.

## 5. Ages: unknown is never 0; first-seen is stable

An unknown age must say "unknown" — 0 reads as "just happened", a false signal either
way. A warning rebuilt fresh every poll must still remember when it was FIRST seen.

**Unknown, never 0.** `dead_letter_items()` already does this correctly
(`_age_seconds_from_iso()`, stamping `age_unknown`). Process-tree holds must adopt the
same convention instead of `_mk_item()`'s ad-hoc `age_seconds=0.0` default. The web
wire layer must forward `age_unknown` for process-tree items exactly as it already
must for the risk register — today `/api/attention` drops it for this source (the
review's P2), so the console reads a bare number as a known, fresh age.

**Stable first-seen.** `_invalid_owned_process_tree_record()` is rebuilt from scratch
every poll with `refreshed_at = now`; nothing persists when a hold was first observed
(why the review found a persistent hold's age stuck near zero forever). Introduce a
small durable **first-seen ledger**: one entry per hold's stable `item_id`, written
once on first observation, read (never overwritten) on every later poll to compute
`age_seconds = now - first_seen_at`. Dropped when the identity resolves; a different
identity (new reason code, new agent) gets its own entry, so this never inherits
another problem's age. Size capped (§7).

## 6. Reader audit

| Reader | Reads today | Changes |
|---|---|---|
| Legacy console `humanQueueCount()` (`console.js:1013`) | wire `count`, unfiltered | **None, if `count` stays active-only.** History items go under a separate wire key (e.g. `history_items`) rather than the counted set — fixes the review's P1 (a lone history row reading as "1 needs a human"). |
| `supervise --reset-process-tree-ownership` (`cli.py:13710`) | projector's raw output, filtered to one match | **None** — per §2 the projector is unchanged. |
| `doctor.py:1167` (`_drop_resolved_dead_letters`) | `item_id` as `dead_letter:<agent>:<msg_id>` | **None**, provided §4's invariant holds (only `dedupe_key` is shared). |
| `attention.py` (`build_queue`, `compute_stats`) | every source's items uniformly | No signature change; new fields (`supervisor_state`, `age_unknown`, shared `dedupe_key`) are handled by existing generic logic. |
| `web.py` (`build_attention`, `build_risk_register`) | full queue, serialized | **Does the real work**: split active/history into separate wire sections, forward `age_unknown`, forward grouped `duplicates`/owners. |
| v2 console (`console2.js`/model, #267/#271) | `/api/attention` items | **New, additive**: render a history section from the new key. The "needs you" open count is already built per-item, not from a count field, so it is naturally active-only; verify explicitly in review. |

**#280** (dead-letter resolve now also closes prior notices) shrinks the backlog this
design groups in the first place — a letter resolved under #280 closes before it ever
reaches §4's age threshold, through the same resolution-fold path `doctor.py:1167`
uses. No conflict.

**#251** (attention from the cached snapshot) is orthogonal: the active/history split
and the first-seen ledger read supervisor state and a small sidecar, not the message
store, so neither adds a per-poll scan. Either can land first.

## 7. Budget

- **History items shown per root:** capped (e.g. 20); beyond it, an explicit "+N more
  in history" note — never a silent truncation, never a reason to cap the active
  section (which this design never caps).
- **Dead-letter groups:** capped per agent-cohort the same way; raw statistics (§4) are
  never subject to this cap.
- **First-seen ledger size** (§5): capped (e.g. a few thousand entries), oldest-entry
  eviction; eviction only resets a stale identity's displayed age, never its
  visibility or severity.
- Owners lists and the "+N more" note reuse the existing `_CAP_*` byte-cap discipline
  (`attention.py:46`) rather than introducing new unbounded strings.

## 8. Acceptance cases

1. An `absent`/`invalid`/unreadable marker shows full severity (unknown ⇒ active),
   never demoted.
2. A `valid` marker whose process is confirmed dead moves to history, age shown.
3. The same hold, if the supervisor restarts, is active again on the very next read.
4. A reused PID is treated as "not running" for the original hold, never as "still
   running".
5. A hold deferred while in history, whose supervisor then restarts, reappears
   unsuppressed at full severity on the next read.
6. Every dead letter past the age threshold still has its own entry in `agenttalk
   attention --stats`'s raw counts, even while grouped for display.
7. Resolving every member of a dead-letter group removes the group; resolving some
   keeps it visible with those gone from `duplicates`.
8. A dead-letter group spanning two agents shows both as owners, not just one.
9. A hold with an unparseable/future `refreshed_at` shows `age_unknown: true` and is
   never sorted as younger than a hold with a known age.
10. A hold persisting across many polls keeps the SAME first-seen age, never resetting.
11. The legacy console's count never includes a history-state item.
12. The v2 console's active list never includes a history-state item.
13. **An unknown supervisor state shows full severity** — the fail-safe default for
    every code path in §1, not only case 1 above.
14. **An active item is never hidden by any history rule** — the only item this design
    ever demotes is a process-tree hold with a confirmed-dead supervisor; every other
    item, and every hold whose state is running or unknown, is untouched throughout.

## 9. Not guaranteed, and open questions

**Not guaranteed:** history changes display urgency, never whether the underlying
condition (a truncated/invalid process tree) still needs a human eventually. The
first-seen ledger is a best-effort anchor, not an audit trail; eviction (§7) can reset
a displayed age, never visibility or severity. Grouping bounds the groups SHOWN, not
the number of distinct agents that could have old dead letters.

**Open questions:**
1. *Age threshold for "old" (§4)?* Recommend keeping the review's own seven days — a
   full week to act, already referenced throughout the findings, no evidence it needs
   to change.
2. *Does history need its own disposition actions?* Recommend reusing the existing
   defer/dismiss/resolve actions unchanged — a second vocabulary has no described need,
   and §3 already makes the existing ones safe across the transition.
3. *Does history poll on the same 2s cadence as active?* Recommend a slower, separate
   cadence (e.g. every 5th active poll, matching `console2.js`'s existing
   `OTHER_ATTENTION_EVERY`) — history changes slowly by definition, and this avoids
   adding request volume to a panel #251 is already working to make cheaper.
