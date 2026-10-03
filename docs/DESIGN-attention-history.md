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
hold now carries a supervisor-state label ("Supervisor not running") and a pile of
old failed messages from one agent collapses into one readable row instead of twenty,
with nothing about its urgency softened, its count, or its individual actionability.

**Build round, final scope (binding — supersedes any conflicting text below):**
- **No new fingerprint.** The existing `source_hash` (`attention.py:1163`) and the
  reset command's comparison (`cli.py:13743`) stay byte-for-byte unchanged. Age and
  the supervisor label are never hash inputs. See §3.
- **No "since" wording, ever.** The supervisor label is exactly one of: "Supervisor
  running", "Supervisor not running", "Supervisor state unknown" — optionally
  "observed not running at `<time>`" when a transition moment is actually known. Never
  "since". See §1.
- **No observed-since cache.** An unknown age shows literally "unknown" — no in-memory
  lower-bound mechanism, no new per-process state at all. See §5.
- **No new browser actions.** Both consoles show per-group-member CLI instructions
  (exact `agenttalk dead-letter ...` / defer commands, full untruncated ids) only.
  Browser buttons for group members are explicitly deferred to a later, separately
  reviewed PR. See §6.
- **Displayed count vs. active count.** The server sends the exact pre-grouping active
  item count; both consoles use it for the "needs a person" tally, never the number of
  displayed rows/groups. See §4, §6.
- This feature adds no new suppression or clearance; existing validated dispositions
  and supervisor/reset lifecycles stay authoritative.

**Fix round 1 (binding — supersedes any conflicting mechanism text in §3–§7
below; a cross-vendor cold read of the build PR, 1 P1 + 7 P2, all with
executed repros):**
- **Grouping is WEB-DISPLAY-ONLY.** §4's description of reusing
  `build_queue()`'s dedupe_key for dead-letter grouping is superseded:
  `attention.py`'s `dead_letter_items()` and its dedupe_key go back to exactly
  master's (ungrouped) behaviour. `agenttalk attention` (CLI, list and show)
  and `/api/risk-register` read that SAME, fully ungrouped projector — they
  never group. Only the `/api/attention` web response groups, as one more
  pass applied AFTER dispositions, over already-serialized wire entries.
- **An explicit typed `group` marker, never a string sharing the id/
  dedupe_key namespace.** The original string-prefix scheme
  (`dead_letter:group:<agent>`) could collide with an ordinary per-message
  key when an agent is literally named `group` — a real, confirmed bug
  (finding 2). A wire entry instead carries a separate `group` object:
  `{kind: "dead_letter_group", agent, member_count, members: [{item_id,
  message_id}, ...], more_count, cli_instructions: [...]}` for a group, or
  `{kind: "dead_letter_overflow", agent_count, member_count,
  cli_instructions: [...]}` for the overflow row. `members`/
  `cli_instructions` are both bounded lists, never a joined paragraph.
- **F1 [P1]: a representative's local defer must never act as a disposition
  of its members.** Both consoles withhold the Later/defer affordance (no
  button, no keyboard shortcut) for any card carrying a `group` marker —
  aggregate cards are CLI-instructions-only, full stop. A stale per-letter
  Later/answered state recorded before a letter became a group's
  representative must not carry over and suppress the group it now
  represents (the console's local disposition check is bypassed entirely for
  a `group`-marked card). The loader uses the server's `active_count`
  directly as the authoritative "needs you" total — never a client-computed
  weighted sum over displayed cards — so deferred, overflowed and
  low-severity members all stay counted even when they are not all
  individually displayed.
- **F5/F6: full ids, complete commands, no truncation.** A group's member
  preview carries both the attention `item_id` (what a defer targets) and
  the raw dead-letter `message_id` (what `dead-letter show`/`resolve`
  targets) — never conflated. `cli_instructions` is a list of complete,
  individually-bounded command lines (not one run-on sentence that could
  need a mid-syntax ellipsis); verified against the longest legal (64-char)
  agent name through the API and both renderers.
- **F7/F8.** The v2 console's overflow card renders its full CLI recovery
  instruction (not just a descriptive sentence). The legacy console's
  unknown-age rendering uses the same riskCard-style branch as every other
  age-unknown item, both at first render and on the 1 Hz clock ticker.

**Fix rounds 2–3 (binding — further narrows §4; a delta read of round 1
closed F1–F4/F6–F8 with no P1):**
- **F9.** A "needs you" count must never be derived from an already
  locally-filtered display list. Console v2's stuck-incident count is
  derived from every agent the console flagged stuck, before any local
  snooze/Later decision — "Wait 10 min" changes what is *shown*, never
  what is *counted*.
- **F5 (closed in v2 too).** A group's bounded member preview (item_id AND
  message_id, each distinguished) must actually render in the DOM, not just
  exist in the model — verified through the real app harness, not the
  model alone.
- **F11.** A synthetic summary row (the overflow row) inserted into an
  already-ranked list must take the ranked position of what it replaces,
  never be appended after the whole list — otherwise a full-severity row
  can render below an unrelated lower-severity item.
- **F10, resolved by scope cut (round 2's `--root`-on-every-command
  approach is withdrawn).** Embedding the displayed root inside each
  generated command broke two ways a review actually exercised: a long
  root pushes a command past its length bound, and POSIX shell-quoting
  (`shlex.quote`) is wrong for PowerShell — a root containing an apostrophe
  splits the command and the CLI exits 2. The fix is not a smarter quoting
  scheme; it is removing the root from the commands entirely. Generated
  commands (list, show, resolve, defer) carry no `--root` and are plain,
  short, and complete — the earlier 64-character-agent-name test already
  proves they fit, with room to spare. Above the commands, each group and
  the overflow row show exactly one line of **prose**, never executable
  syntax: `Run these from the project folder: <path>`. Being prose, this
  line may ellipsize at its bound; a command never may. The reader runs
  the commands from that folder (or with their own usual
  `--root`/`$AGENTTALK_ROOT`) — shell-neutral, and never truncated
  regardless of how long or unusual the root is.

What you need to do: nothing yet — docs only. This is the contract later
implementation PRs must satisfy and be reviewed against.

---

## 1. Supervisor state: a label, never a gate

**Why this changed:** round 1 planned to use "supervisor not running" as proof a HOLD
could be shown less prominently. A review found the flaw: a dead supervisor is not
proof its held processes stopped too — they can keep running, unattended, which is
exactly when a human is needed most. So this state is now **only ever shown as
context text on the hold**, never used to move, demote, or uncount anything (see §2).

**The three states**, computed fresh on every read, with exact wording (build round —
never a "since `<time>`" form):
- **running** → "Supervisor running" — a valid marker names a process, confirmed alive
  and confirmed to be the SAME process that wrote the marker (not a different process
  reusing its PID).
- **not running** → "Supervisor not running" — a valid marker names a process
  confirmed dead, or confirmed to now be a different process (PID reused). May
  optionally append "observed not running at `<time>`" when a specific observation
  moment is actually known — never a "since" phrasing, which implies a continuous
  duration this design does not track.
- **unknown** → "Supervisor state unknown" — an unreadable/invalid marker, an
  inconclusive liveness probe, or the marker is missing a usable `pid_start` — the
  label never guesses "not running".

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

## 3. No new fingerprint (F1, CUT in the build round)

Round 1 proposed folding active/history state into the defer fingerprint; round 2 kept
a version of that idea re-scoped to "one shared canonical fingerprint" under F4. A
final design read found this was still the one remaining blocker: changing
`source_hash` or the reset command's admission comparison is exactly the kind of change
that could silently break an in-flight defer or an in-flight reset across the cutover,
for a benefit (one shared identity concept) that v1 does not need.

**Decision: cut it.** v1 makes no change at all to either comparison:
- `attention.py:1163`'s existing `source_hash` construction stays **byte-for-byte
  unchanged** — same fields, same order, same hash.
- `cli.py:13743`'s reset command's admission comparison stays **byte-for-byte
  unchanged**.
- Age and the supervisor-state label (§1) are never hash inputs, now or later — this
  was already true, and v1 adds nothing that would change it.

A defer recorded today keeps matching exactly as it does today; the reset command's
own independent checks are completely untouched by this feature. If a genuinely shared
fingerprint is wanted later, it is its own, separately reviewed change — not bundled
into a display/grouping feature.

## 4. Grouping old failed (dead-letter) messages

**Grouping compresses display; it never hides, demotes, or uncounts.** Every group
(and every message inside it) is active and full-severity for as long as it exists.

- **Group per agent.** One group per agent, never a group spanning agents — this
  removes the "whose fault is this" ambiguity a shared group used to create, by
  construction, rather than by picking a representative owner.
- **Only letters with a known, finite age at or beyond a named threshold — 604800
  seconds (7 days)** — are eligible for grouping. A letter with a recent, missing,
  invalid, or future-dated age keeps its own full row, always — age uncertainty is
  never read as "old enough to group". No other source is ever capped or grouped this
  way; this applies to dead letters only.
- **Dispositions apply before grouping.** A resolved or deferred letter leaves the
  active set the same way it does today, and is never grouped — grouping only ever
  sees what's still active. A group with zero remaining members is simply absent, not
  an empty/zero row.
- **A group shows:** its total member count; a bounded preview of members (named
  default: **5**, deterministic selection — e.g. oldest-first by the same ordering used
  elsewhere, so the preview is stable across polls); the one owning agent; and an exact
  "N more" count for anything past the preview, **computed before any truncation** (not
  an approximation derived from the capped list). Member and preview ids are **never
  truncated** — a person must be able to copy one verbatim into a CLI command. Preview
  text is bounded UTF-8-safe (reuses `_CAP_*`, §7).
- **Concrete CLI instructions accompany every group and every overflow row** — the
  exact `agenttalk dead-letter show <agent> <message_id>` / `agenttalk attention defer
  ...` / `agenttalk dead-letter resolve ...` commands (full ids) needed to reach an
  unpreviewed member or an overflowed agent, so nothing past the preview cap is ever
  unreachable, only unlisted.
- **Caps on the groups themselves: 20 agent groups shown** (named default). If there
  are more per-agent groups than that, the excess collapses into exactly one further
  active row — "N more agents have old failed messages" with the true total count
  across them, computed before truncation. This row is **full-severity and active**,
  counted the same as everything else, but **not itself actionable as a group** — it
  is a pointer to the CLI, not a defer/resolve target. This row is never empty and
  never silently absent; it is the one place "nothing drops to zero" is guaranteed even
  at the extreme. Selection of which 20 groups are shown (vs. collapsed into the
  overflow row) is deterministic, same ordering as the member preview.
- **Resolve and defer are never group-wide.** Both apply to one original `item_id` and
  its own source snapshot, exactly as today — grouping is a read/display projection
  only, with no bulk action implied. **Raw statistics (`compute_stats`) count every
  letter**, grouped or not, unchanged from today. Per-letter stats are untouched by any
  of this.
- **Displayed count vs. active count (build round).** The number of rows/groups shown
  is a display detail; it must never be confused with the number of active items. The
  server computes and sends the exact pre-grouping active item count (after
  dispositions), and both consoles use that value for the "needs a person" tally — not
  `len(wire)`, not a count of displayed groups. Example: 20 individually-failed letters
  from one agent, all past the age threshold, become 1 displayed group — the active
  count stays 20. The risk register uses the same active-count semantics as the
  attention payload; neither double-counts a group's members against its own preview
  rows.
- **Response-byte budget:** the member preview and any owner/diagnostic text reuse the
  existing `_CAP_*` byte-cap discipline (`attention.py:46`) rather than introducing new
  unbounded strings; see §7 for the exact caps.

## 5. Ages: durable evidence, or "unknown" — never a silent guess

An age is shown from the item's own durable evidence when that evidence is valid (a
dead letter's recorded `deadlettered_at`; a hold's own recorded timestamps). When
there is no valid durable evidence, the age is **"unknown"**, never `0` — `0` reads as
"just happened", which is a false signal in exactly the wrong direction. Never present
`refreshed_at`, a lock's own startup time, or any other clock-derived field that isn't
genuine incident age as if it were the age.

**No new durable ledger (F2, reversing round 1) — and no in-memory cache either
(build round, reversing round 2).** A first-seen *file* was round 1's proposal to fix a
hold whose evidence resets every poll; round 2 replaced it with an in-memory
"observed since `<time>`" lower bound instead. The build round cuts that too: it is
still new per-process state for one display field, with its own lifecycle question
(what happens on restart, under multiple console instances, etc.) that v1 does not
need to answer. **v1 shows literally "unknown"** — no lower bound, no observation
timestamp, no new state of any kind, in memory or on disk. **A process-tree HOLD's age
defaults to unknown** unless a genuine durable timestamp exists for it.

**Ranking unknown ages (F7).** An item whose age is unknown is never sorted as if it
were young: give it an explicit, conservative rank — sort it as the OLDEST in its tier,
matching the existing `age_unknown` → sort-as-infinite convention already used
elsewhere in this codebase (`web.py`'s risk register), not a new convention.

## 6. Reader audit

| Reader | Reads today | Changes |
|---|---|---|
| Legacy console `humanQueueCount()` (`console.js:1013`) | wire `count`, unfiltered | **Changes (build round):** must use the server-sent pre-grouping `active_count`, not `len(wire)` — `len(wire)` now undercounts once grouping collapses rows. |
| `supervise --reset-process-tree-ownership` (`cli.py:13710`/`:13743`) | projector's raw output, filtered to one match; admission comparison | **None** — §2 leaves the projector untouched; §3 leaves the admission comparison byte-for-byte untouched. |
| `doctor.py:1167` (`_drop_resolved_dead_letters`) | `item_id` as `dead_letter:<agent>:<msg_id>` | **None** — grouping (§4) shares only a display key, never `item_id`. |
| `attention.py` (`build_queue`, `compute_stats`) | every source's items uniformly | Gains a supervisor-state label field and a per-agent grouping key; no signature change to `source_hash` (§3), no new filtering logic (nothing is newly excluded). |
| `web.py` (`build_attention`, `build_risk_register`) | full queue, serialized | Forwards the supervisor-state context label, `age_unknown`, grouped member previews/owner/"N more"/CLI instructions for dead letters, and the pre-grouping **`active_count`** (build round) so consoles stop relying on `len(wire)`. Risk register uses the same active-count semantics — no double-counting between a group and its preview rows. |
| Both consoles (legacy and v2) | `/api/attention` items | **Must render the grouping and the active count** (F5, build round): both consoles show **CLI instructions only** for group members (no new browser buttons — that's deferred to a later PR) and use the server's `active_count` for the "needs a person" tally. **Action permissions are unchanged**: a HOLD still allows `defer` only (`_ALWAYS_BLOCKING`); dead letters still allow `defer` and `resolve`, never `dismiss` — grouping changes no permission. |

**#280** (dead-letter resolve now also closes prior notices) still shrinks the backlog
this design groups, upstream of §4's threshold — no conflict. **#251** (attention from
the cached snapshot) is still orthogonal: nothing here adds a per-poll disk scan.

**One atomic response (F7).** The supervisor-state labels and the dead-letter grouping
are computed from the same underlying read that builds the rest of the `/api/attention`
payload — never two reads that could disagree with each other inside one response.

## 7. Budget

Named defaults (build round):
- **Agent groups shown: 20.** Excess collapses into one overflow row carrying the true
  total (§4); raw statistics (§4) are never subject to this cap.
- **Member preview per group: 5 ids.** The exact "N more" count is always computed
  before truncation, never an approximation derived from the capped list.
- **Text bytes:** owner names, preview ids, and the overflow summary line all reuse
  the existing `_CAP_*` caps (`attention.py:46`), bounded UTF-8-safe; no new unbounded
  string is introduced anywhere in this design. Full ids (used in CLI instructions)
  are never subject to this truncation — only free-text/diagnostic fields are.
- Process-tree HOLDs are never capped — §2 already forbids excluding one from the
  active list for any reason, and a cap would be exactly that.
- Selection of which groups/members are shown vs. collapsed is deterministic (stable
  across polls for the same underlying data), never arbitrary or order-dependent on
  map/dict iteration.

## 8. Acceptance cases

1. **A dead supervisor with live descendants keeps its hold active at full
   severity** — a HOLD whose supervisor is confirmed dead stays active, counted,
   labeled "Supervisor not running" (optionally "observed not running at `<time>`",
   never "since"), never moved or demoted.
2. A HOLD for a retired agent stays active exactly the same way — retirement is no
   more proof of a stopped process than supervisor death is.
3. A HOLD whose marker is `absent`/`invalid`/unreadable, missing `pid_start`, or
   containing invalid UTF-8, is labeled "Supervisor state unknown" (from an isolated,
   independent failure path) and stays active, same as every other HOLD.
4. The only way a HOLD leaves the active list is a successful
   `--reset-process-tree-ownership`; nothing in this panel's own logic clears one.
5. `source_hash` (`attention.py:1163`) and the reset command's admission comparison
   (`cli.py:13743`) are byte-for-byte unchanged by this feature; a defer recorded
   today keeps matching exactly as it does today.
6. A dead letter with a recent, missing, invalid, or future-dated age is never swept
   into a group — it keeps its own full row.
7. A dead letter past the 604800-second (7-day) age threshold joins its agent's (and
   only its agent's) group, active, full severity, counted; a group never spans two
   agents.
8. Resolving every member of a group removes it from view (the group is absent, not
   an empty row); resolving some members keeps it visible with an accurate remaining
   count and preview, computed before truncation.
9. A dead-letter group names its member ids (bounded preview of 5, full untruncated
   ids), an exact "N more" count, and the concrete CLI commands (with full ids) to
   reach an unlisted member.
10. More than 20 per-agent groups collapse into one "N more agents" row carrying the
    true total — never an empty or missing summary; this row is full-severity and
    active but not itself actionable as a group.
11. `agenttalk attention --stats` counts every dead letter individually, grouped or
    not; per-letter stats are unaffected by grouping.
12. An item with an unknown age is ranked as the oldest in its tier, never as if it
    were new. A hold or letter with no durable age evidence shows literally "unknown"
    — no in-memory lower bound, no "observed since" label, no new persisted or
    per-process state of any kind.
13. **20 individually-failed letters from one agent, all past the age threshold,
    become exactly 1 displayed group — the server's `active_count` stays 20.** Neither
    console mistakes the 1 displayed row for the active total.
14. **A multi-agent overflow example:** 25 agents each have old failed letters (25
    groups would result); the panel shows 20 groups plus one overflow row reading "5
    more agents have old failed messages"; `active_count` reflects the true total
    number of individual dead letters across all 25 agents, not 21 (20 groups + 1
    overflow row) and not 25 rows shown.
15. Both consoles render dead-letter grouping and the supervisor-state label using
    CLI-instructions-only for group members (no new browser buttons); the legacy
    console's count and the v2 console's active list both use the server's
    `active_count`, unaffected by grouping or supervisor-state labeling.
16. Defer remains the only action available on a process-tree HOLD; resolve and
    defer on a dead letter stay scoped to one original item, never group-wide.
17. The panel text states plainly that this feature adds no new suppression or
    clearance — existing validated dispositions and supervisor/reset lifecycles stay
    authoritative.
18. **(H1 regression)** A defer recorded against a HOLD does not hide a *different*
    new HOLD that happens to share grouping/display characteristics but differs in
    launch id, generation, or identity evidence — the stale defer must not suppress
    the new, distinct problem.
19. **(H2 regression)** A reset admission check computed against a stale snapshot
    hash refuses to apply when the current hold's hash no longer matches — the reset
    command never clears a hold it wasn't actually shown.

## 9. Not guaranteed, and open questions

**Not guaranteed:** this design never promises a HOLD resolves itself — only the
reset command's own checks do that, unchanged. An "unknown" age is exactly that —
unknown — never a disguised lower bound; v1 tracks no observation-time state at all.
The per-agent group cap (20) bounds groups *shown*, not how many agents could have old
letters; the member preview cap (5) bounds what's listed, not what's reachable (every
member stays reachable via the accompanying CLI instructions).

**Resolved in the build round (no longer open):**
1. *Age threshold for "old" (§4)?* **Decided: 604800 seconds (7 days).**
2. *Exact wording of the supervisor-state context label?* **Decided:** "Supervisor
   running" / "Supervisor not running" (optionally "observed not running at
   `<time>`") / "Supervisor state unknown" — plain, factual, never "since".
3. *Does the reset command's own admission hash need to literally share code with a
   new fingerprint?* **Moot — §3 cuts the new fingerprint entirely.** The reset
   command's admission comparison is untouched, independent, and shares nothing new
   with this feature.
