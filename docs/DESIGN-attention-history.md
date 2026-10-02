# Design: attention panel history (stale supervisor warnings, old failed messages)

## In plain words

The console's "what needs you" panel should show only what really needs a person.
Today it can also show warnings left behind by a supervisor that already stopped, and
old failed messages that pile up without end. Two earlier attempts to tame this
(PR #272, and round 1 of this document) both tried to make some of that information
less prominent — and both times, a review found the exact way that happened could hide
a real, current safety problem. This round removes every mechanism that could lower or
uncount a warning, keeping only mechanisms that compress how something is *shown*
without ever changing whether it's active, counted, and at full severity.

**The one rule, applied without exception this time:** nothing is ever moved, demoted,
or left out of the count because a supervisor died, an agent retired, or time passed.
A process-tree HOLD clears only when the existing reset command says so, with its own
independent checks. Supervisor state becomes a *label*, never a reason to lower
anything. Old failed messages may be *grouped* for a shorter display, but a group is
active, counted, full-severity, and every message inside it stays resolvable on its own.

What you'll notice: the panel looks almost the same as today, except a process-tree
hold now says *why* it's still open ("supervisor not running since ...") and a pile of
old failed messages from one agent collapses into one readable row instead of twenty,
with nothing about its urgency softened.

What you need to do: nothing yet — docs only. This is the contract later
implementation PRs must satisfy and be reviewed against.

---

## 1. Supervisor state: a label, never a gate

**Why this changed:** round 1 planned to use "supervisor not running" as proof a HOLD
could be shown less prominently. A review found the flaw: a dead supervisor is not
proof its held processes stopped too — they can keep running, unattended, which is
exactly when a human is needed most. So this state is now **only ever shown as
context text on the hold** ("supervisor not running since 14:02"), never used to move,
demote, or uncount anything (see §2).

**The three states**, computed fresh on every read:
- **running** — a valid marker names a process, confirmed alive and confirmed to be
  the SAME process that wrote the marker (not a different process reusing its PID).
- **not running** — a valid marker names a process confirmed dead, or confirmed to now
  be a different process (PID reused).
- **unknown** — an unreadable/invalid marker, an inconclusive liveness probe, or the
  marker is missing a usable `pid_start` — the label says "supervisor state unknown",
  never guesses "not running".

**Technical detail.** Read `supervisor.instance.lock` with
`Store.read_supervisor_instance_strict()` (`absent`/`valid`/`invalid`) — never
`read_supervisor_instance()`, which collapses `invalid` into the same `None` as
`absent`. Both map to **unknown**. For a `valid` marker, call the module-level function
`store._probe_owner_identity(pid, pid_start)` (**F6**: it is a module function in
`store.py`, not a `Store` method — round 1 cited it wrong). It checks raw liveness,
then for an alive PID compares the process's creation time against the marker's
recorded one; a mismatch means the original process is gone. Map its outcomes:
`ALIVE` → running; `DEAD`/`PID_REUSED` → not running; anything else (including a
missing/invalid `pid_start`, invalid UTF-8 in the marker file, or any raised
exception) → unknown. Read the marker and probe the PID in two separately-guarded
steps — a failure in one must never be conflated with, or silently swallow, the other.

## 2. There is no "history" for process-tree HOLDs

A HOLD is never moved, demoted, or uncounted for any reason — not supervisor death,
not retirement, not age. It stays in the active list, at its original severity,
counted, for as long as it exists. The supervisor-state label from §1 rides alongside
it as context, nothing more. **The only way a HOLD clears is the existing
`agenttalk supervise --reset-process-tree-ownership` command**, which has its own,
independent evidence requirements (stopped-identity proof, admission checks) completely
unrelated to this panel's display logic — this design does not touch, weaken, or
duplicate those checks. `process_tree_hold_items()` and every caller of it
(`cli.py:13710` included) are unaffected: the projector already returns every current
hold to every caller today, and continues to.

This directly closes three review findings at once: a dead supervisor's HOLD is never
hidden because its descendants might still be running; a retired agent's HOLD is never
hidden because retirement alone is no more proof of a stopped process than supervisor
death is; and a HOLD whose instance marker is merely unreadable was never a candidate
for hiding in the first place, since nothing here ever hides a HOLD.

## 3. One fingerprint, shared, never ticking (F4)

Round 1 proposed folding active/history state into the defer fingerprint so a stale
disposition couldn't survive a transition that, as of §2, no longer exists for HOLDs.
What's still needed, re-examined under D1: **one canonical identity fingerprint for a
process-tree hold, computed the same way everywhere it's compared** — by the web
collector building the attention wire, by CLI disposition capture (`agenttalk attention
defer`), and by the reset command's own admission check that it is still looking at the
same problem it was told about. All three must agree on what "the same hold" means, or
a defer recorded through one path could silently fail to match a read through another.

The fingerprint is built from the hold's stable identity content — agent, the
structural reason/status fields — and **must never include a ticking value** (`age`,
`refreshed_at`, any clock-derived field): a hash that changes every poll can never stay
matched long enough for a defer to apply at all, which would make deferring a hold
look broken rather than working as designed.

## 4. Grouping old failed (dead-letter) messages

**Grouping compresses display; it never hides, demotes, or uncounts.** Every group
(and every message inside it) is active and full-severity for as long as it exists.

- **Group per agent.** One group per agent, never a group spanning agents — this
  removes the "whose fault is this" ambiguity a shared group used to create, by
  construction, rather than by picking a representative owner.
- **Only letters with a known, finite age at or beyond a named threshold** (recommend
  keeping the review's own seven days, §9) are eligible for grouping. A letter with a
  recent, missing, invalid, or future-dated age keeps its own full row, always — age
  uncertainty is never read as "old enough to group".
- **A group shows:** its total member count; a bounded preview of members (enough to
  act on without opening anything else); the one owning agent; and an exact "N more"
  count for anything past the preview. Nothing in a group is ever left unaccounted.
- **Overflow of groups themselves:** if there are more per-agent groups than the panel
  can show, the excess collapses into exactly one further active row — "N more agents
  have old failed messages" with the true total count across them. This row is never
  empty and never silently absent; it is the one place "nothing drops to zero" is
  guaranteed even at the extreme.
- **Resolve and defer are never group-wide.** Both apply to one original `item_id` and
  its own source snapshot, exactly as today — grouping is a read/display projection
  only, with no bulk action implied. **Raw statistics (`compute_stats`) count every
  letter**, grouped or not, unchanged from today.
- **Response-byte budget:** the member preview and any owner/diagnostic text reuse the
  existing `_CAP_*` byte-cap discipline (`attention.py:46`) rather than introducing new
  unbounded strings; see §7 for the exact caps.

## 5. Ages: durable evidence, or "unknown" — never a silent guess

An age is shown from the item's own durable evidence when that evidence is valid (a
dead letter's recorded `deadlettered_at`; a hold's own recorded timestamps). When
there is no valid durable evidence, the age is **"unknown"**, never `0` — `0` reads as
"just happened", which is a false signal in exactly the wrong direction.

**No new durable ledger (F2, reversing round 1).** A first-seen *file* was round 1's
proposal to fix a hold whose evidence resets every poll; it added a new persisted
reader/writer for one display field, which is more machinery than the problem needs.
Instead: if the only thing known is when *this running console* first noticed the
exact identity, it may say **"observed since &lt;time&gt;"**, kept in memory only, for
the life of the current server process — explicitly labeled as a **lower bound**, not
a claim about the true age, since the problem could predate this console's own
observation. Nothing is written to disk; there is no new reader.

**Ranking unknown ages (F7).** An item whose age is unknown is never sorted as if it
were young: give it an explicit, conservative rank — sort it as the OLDEST in its tier,
matching the existing `age_unknown` → sort-as-infinite convention already used
elsewhere in this codebase (`web.py`'s risk register), not a new convention.

## 6. Reader audit

| Reader | Reads today | Changes |
|---|---|---|
| Legacy console `humanQueueCount()` (`console.js:1013`) | wire `count`, unfiltered | **None.** Since §2/§4 mean nothing is ever excluded from active/counted, the existing `count = len(wire)` meaning is unchanged. |
| `supervise --reset-process-tree-ownership` (`cli.py:13710`) | projector's raw output, filtered to one match | **None** — §2 leaves the projector, and every one of its callers, untouched. |
| `doctor.py:1167` (`_drop_resolved_dead_letters`) | `item_id` as `dead_letter:<agent>:<msg_id>` | **None** — grouping (§4) shares only a display key, never `item_id`. |
| `attention.py` (`build_queue`, `compute_stats`) | every source's items uniformly | Gains a supervisor-state label field and a per-agent grouping key; no signature change, no new filtering logic (nothing is newly excluded). |
| `web.py` (`build_attention`, `build_risk_register`) | full queue, serialized | Forwards the supervisor-state context label, `age_unknown`, and grouped member previews/owner/"N more" for dead letters (**F5**). |
| Both consoles (legacy and v2) | `/api/attention` items | **Must render the grouping** (F5): the v2 console with per-member actions (expand a group to defer/resolve one message); the legacy console with exact CLI instructions where a rich per-member UI doesn't exist. **Action permissions are unchanged**: a HOLD still allows `defer` only (`_ALWAYS_BLOCKING`); dead letters still allow `defer` and `resolve`, never `dismiss` — grouping changes no permission. |

**#280** (dead-letter resolve now also closes prior notices) still shrinks the backlog
this design groups, upstream of §4's threshold — no conflict. **#251** (attention from
the cached snapshot) is still orthogonal: nothing here adds a per-poll disk scan.

**One atomic response (F7).** The supervisor-state labels and the dead-letter grouping
are computed from the same underlying read that builds the rest of the `/api/attention`
payload — never two reads that could disagree with each other inside one response.

## 7. Budget

- **Dead-letter groups:** capped per agent the same way other lists in this payload
  are; raw statistics (§4) are never subject to this cap.
- **Member preview per group:** a small, fixed cap (e.g. 5 ids); the exact "N more"
  count always makes up the difference, never an approximation.
- **Text bytes:** owner names, preview ids, and the overflow summary line all reuse
  the existing `_CAP_*` caps (`attention.py:46`); no new unbounded string is
  introduced anywhere in this design.
- Process-tree HOLDs are never capped — §2 already forbids excluding one from the
  active list for any reason, and a cap would be exactly that.

## 8. Acceptance cases

1. **A dead supervisor with live descendants keeps its hold active at full
   severity** — a HOLD whose supervisor is confirmed dead stays active, counted,
   labeled "supervisor not running since ...", never moved or demoted.
2. A HOLD for a retired agent stays active exactly the same way — retirement is no
   more proof of a stopped process than supervisor death is.
3. A HOLD whose marker is `absent`/`invalid`/unreadable, missing `pid_start`, or
   containing invalid UTF-8, is labeled "supervisor state unknown" (from an isolated,
   independent failure path) and stays active, same as every other HOLD.
4. The only way a HOLD leaves the active list is a successful
   `--reset-process-tree-ownership`; nothing in this panel's own logic clears one.
5. A defer recorded against a hold's fingerprint keeps applying only while that exact
   fingerprint (agent + reason/status content, no ticking field) still matches.
6. A dead letter with a recent, missing, invalid, or future-dated age is never swept
   into a group — it keeps its own full row.
7. A dead letter past the age threshold joins its agent's (and only its agent's)
   group, active, full severity, counted; a group never spans two agents.
8. Resolving every member of a group removes it from view; resolving some members
   keeps it visible with an accurate remaining count and preview.
9. A dead-letter group names its member ids (bounded preview) and an exact "N more" —
   a person can act on an unlisted member via that count and the CLI.
10. More per-agent groups than the panel cap collapse into one "N more agents" row
    carrying the true total — never an empty or missing summary.
11. `agenttalk attention --stats` counts every dead letter individually, grouped or
    not.
12. An item with an unknown age is ranked as the oldest in its tier, never as if it
    were new; a hold with no durable age evidence, newly observed by this console, is
    labeled "observed since &lt;time&gt;" — not a bare "unknown", not a persisted file.
13. Both consoles render dead-letter grouping; the legacy console's count and the v2
    console's active list are unaffected by grouping or supervisor-state labeling.
14. Defer remains the only action available on a process-tree HOLD; resolve and
    defer on a dead letter stay scoped to one original item, never group-wide.

## 9. Not guaranteed, and open questions

**Not guaranteed:** this design never promises a HOLD resolves itself — only the
reset command's own checks do that, unchanged. A console's "observed since" label is a
lower bound, not the true age, and resets whenever that console process restarts. The
per-agent group cap bounds groups *shown*, not how many agents could have old letters.

**Open questions:**
1. *Age threshold for "old" (§4)?* Recommend keeping seven days, as before — no new
   evidence it needs to change, and operators already expect it from the review
   findings.
2. *Exact wording of the supervisor-state context label?* Recommend "supervisor not
   running since `<time>`" / "supervisor running" / "supervisor state unknown" —
   plain, factual, no urgency language of its own (the HOLD's own severity already
   carries that).
3. *Does the reset command's own admission hash need to literally share code with
   §3's fingerprint, or just agree on content?* Recommend agreeing on content only
   (same fields, independently computed) rather than a forced shared function —
   the reset command's checks are intentionally independent of this panel's display
   logic, and a shared *function* would blur that boundary for no described benefit.
