# write-for-humans: before and after samples

These two samples show the write-for-humans skill applied to real text from this
project, so you can compare the two styles side by side.

- **The "before":** each one is copied exactly as it is today, inside a box, so
  nothing in it has changed.
- **The "after":** the same text rewritten in the new style. It keeps every fact,
  explains them in plain words, and moves file names, commit IDs and test names
  into a "Technical details" section at the end. Its headings are shifted down
  two levels so they fit inside this page.

These are examples for judging the style. They are not part of the skill.

## Sample 1: the release notes for version 0.94.0

### Before (exactly as in CHANGELOG.md)

````markdown
## [0.94.0] - 2026-09-28

Theme: **a work board that moves itself, and seats that survive a busy store.**

### Added

- **Console v2 preview at `/v2` (pitch slice, step M1).** A second, read-only
  document route beside the classic `/dashboard`: shell, theme engine (Midnight
  styled; Paper, Synthwave and Terminal carry their variables), header with team
  chips, and the agent-name shortener (`claude-agenttalk-frontend-dev` shows as
  `fe-dev`). It serves three new allowlisted assets (`console2.css`,
  `console2-model.js`, `console2.js`) under the unchanged console
  Content-Security-Policy: system fonts, no inline style or script, bus content
  only through `textContent`. The classic console gains a "New console" link and
  `agenttalk dashboard` prints the `/v2` URL; `/` and `/dashboard` are unchanged.
  The preview polls the existing feeds (`/api/state`, `/api/attention`,
  `/api/lead-chat`, GET only) and derives, with node-tested pure functions,
  the greeting, the needs-you queue with evidence, stuck-vs-busy with the
  heartbeat/progress/last-message fallback, the roster with a "down" state,
  usage windows per runtime, and two separate offline banners ("can't reach the
  console server" vs "no agent has reported for over 5 minutes"). Keyboard focus
  stays put in the header and `?root=` selects the team (an unknown one is
  stated, never replaced). Plan and open items: `docs/STEP-CONSOLE-V2-PITCH.md`.

- **Health reads that are too old now say what they last reported.** A
  snapshot older than the TTL, or older than the heartbeat by more than the
  skew, still reads `state: unknown` with every existing field unchanged, and
  additionally carries `last_known_state`, `last_known_since`,
  `last_known_updated_at` and (when present) `last_known_progress_at`. Nothing
  else changes for status, doctor, the supervisor or the classic console; the
  v2 console uses the fields to tell a wedged silent turn from a healthy one.

- Add the pure work-board reducer: validated envelopes become work items placed by
  the design's precedence rows from explicit correlation only, with a counted legacy
  group. No bodies, subjects or message-ID order are used; unknown is never green.
  Adversarial hardening keeps needs-info HOLDs, follows explicit replacement chains,
  compares whole repository/check policies and degrades malformed or partial history
  to a per-item Unknown instead of a false Ready.

- Discover all compacted work evidence through the shared snapshot, caching validated
  facts across polls and keeping active state available while archive coverage builds or degrades.

- Share validated active envelopes across dashboard polls through a per-root
  worker snapshot, with generation checks and a cross-partition selected-work budget.

- Configure model vendors independently of CLI transport; freeze per-recipient
  vendor maps on work dispatches and link escalations to exact work items/cycles.

- Serve a bounded, read-only work-board feed from the shared snapshot worker, with
  exact item/cycle/revision checks and visible coverage, overflow and legacy groups.

- Extract the merged console v2 needs-you calculation into one shared projection
  (`console2-model.js`) so a future work-board view reads the exact same incident
  identity as the stream: escalation `source_refs` carry the validated item/cycle
  (never guessed), attention and lead-chat pending decisions dedupe by escalation
  ID, and Later/Wait defer local presentation only, never the underlying incident.

- **Read-only work board at `/v2#board`.** A second view in the same console v2 app,
  behind one router that also owns `#conversation` and a reserved `#review=<escalation-id>`
  (no UI yet). Its pure model (`console2-board-model.js`) projects `/api/work-board`
  cards on top of the shared B7 needs-you projection - never recomputed - showing
  title/reason, seats, configured model vendor or "unverified", explicit round or
  unknown, open/total obligations, approximate dispatch/activity ages and the
  evidence behind each placement; findings, cost and merge stay explicitly Unknown
  where the data does not prove them. Keyed cards are reconciled in place, keeping
  focus and scroll across redraws, with j/k/Escape selection and a read-only detail
  panel. Both route anchors are fixed, server-authored hash links, so switching
  views never disturbs `?root=`.

### Fixed

- Keep the fresh last-known-good dashboard view during snapshot scan races and
  schedule a bounded prompt retry; config mismatch and stale data still refuse.

- Recheck current task authority and reader compatibility when resuming a partial
  task broadcast; a prior dispatch no longer authorizes a demoted sender to resume.

- Keep wrapped seats alive through store lock contention (#154): the continuous loop
  retries only the contended step in place, before admission or, after a completed
  turn, for publication (one stable operation nonce) and finalization, never
  re-driving the turn, including on owed (admitted-dispatch) heads. Contention shows
  as `rate_limited_or_outage` / `store_lock_contention`, with one wrapper-log
  diagnostic per persistent episode. An owner that cannot re-enter a lock's guard
  to remove its own marker no longer waits on itself: the process removes exactly
  that marker on its next acquisition or sweep. Contention is never persisted as a
  permanent block, and no gate method raises anything new because of it:
  failed-delivery disposition, retry-exhaustion settlement, block writes and
  cursor-projection accounting keep their own internal retry and return state, but
  a contended outcome is left unpersisted, so the step is retried (in place in the
  continuous loop, on the next poll in a one-shot). Corruption, access denial, unsafe
  generation changes,
  lock-order inversions and a lost lease keep their existing handling and are
  never retried as contention.

- Isolate `wb.` gates from root checks and both gate-HOLD attention paths; refuse
  new board names in root requirements and warn about legacy contaminated lists.
````

### After

#### [0.94.0] - 2026-09-28

**In short:** this release adds the first preview of a new, clearer console, and
builds the groundwork for a "work board" that shows every piece of work and where
it stands, with nobody moving cards by hand. It also stops agents from stopping
when the shared message store is busy.

##### New

**A preview of the new console**

The console is the web page where you watch your team of AI agents at work. This
release adds a first preview of a redesigned console, next to the old one.

Why it matters: the old console shows a lot of raw detail. The new one is meant to
answer "what needs me right now?" at a glance.

What you will notice:
- **Finding it:** the old console has a new "New console" link, and
  `agenttalk dashboard` also prints the new address.
- **What it shows:** the new page greets you and lists the things waiting for
  your decision, each with the evidence behind it. It shows which agents are
  working, which seem stuck, which are just busy and which are not responding.
  It also shows how much of each agent's usage allowance is left.
- **Two kinds of trouble, kept apart:** it shows two separate warnings, one when
  the console itself cannot be reached and one when no agent has reported in for
  five minutes.
- **Look and feel:** long agent names are shortened, so you see "fe-dev" instead
  of the full name. There are several colour themes; one is finished and the
  others are prepared.
- **Safe by design:** the new page follows the same strict safety rules as the
  old one. It only reads data, never runs anything that arrives in a message, and
  shows message text exactly as text.
- **Nothing else moves:** the old console and its addresses do not change.

What you need to do: nothing. Open the new link if you want to try it.

**When an agent's health report is too old, you now see what it last said**

Every agent regularly reports how it is doing ("working on a task", "waiting for
work" and so on). When a report is too old to trust, the console has always shown
"unknown". It still does, but now it also shows the last thing the agent reported,
and when.

What you will notice: next to "unknown" you can now see, for example, "last known:
working on a task, since 20 minutes ago". This helps you tell an agent that is
stuck in a long, silent task from one that is fine. Nothing else changes.

**The work board, first parts (mostly behind the scenes)**

The work board will show each piece of work, such as "build this feature" or
"review that change", in a column like Queued, Building, In review or Ready. It
works out the column by itself from the messages the agents exchange, so nobody
has to update it. This release ships the parts that make that possible, plus a
first view you can look at:
- **Sorting work into columns:** the logic that reads the agents' messages and
  decides which column each piece of work belongs in. It uses only information
  the agents recorded on purpose and never guesses from the wording. When the
  history is incomplete or contradictory, the card says "unknown" instead of
  pretending the work is ready.
- **Old messages count too:** the board also reads older, archived messages, so
  old work does not vanish from it. The console stays responsive while that
  reading happens.
- **One shared copy:** all the console's views share one checked copy of the
  messages, instead of each one reading everything again.
- **Which company's model:** each agent can now be recorded with the company
  whose AI model it uses, for example Anthropic or OpenAI. This is kept separate
  from the program that runs it, so it can be checked that a review came from a
  different company than the work it reviews. Questions raised to you
  ("escalations") are now linked to the exact piece of work they are about.
- **A data feed:** a limited-size feed gives the board's data to the console,
  and says clearly what it could and could not see.
- **One answer to "what needs you":** a single shared calculation of what needs
  you, so the board and the conversation view always agree. Choosing "Later" or
  "Wait" on a question only hides it on your screen. The question itself stays
  open.
- **A first look at the board:** a read-only board view in the new console.
  - **On each card:** the title, who is involved, the review round, how many
    tasks are still open, roughly how long ago things happened, and the evidence
    for the column it is in.
  - **Honest gaps:** where the data does not prove something, such as whether
    the work was added to the main version, the card says "unknown".
  - **Moving around:** use the j, k and Escape keys, and open a details panel.
    The board keeps your place when it refreshes.

What you will notice: a "Board" view in the new console. You can look, but not
change anything.

##### Fixed

**The console no longer goes blank for a moment during busy periods.** Before
this, if the console read the messages at the same moment new ones arrived, it
could briefly show no data. Now it keeps showing the last good view and quickly
tries again. It still refuses to show data it knows is out of date, or data from
a different setup.

**A lead that lost its role can no longer finish sending a task.** A "broadcast"
is one message sent to several agents at once. If sending was interrupted and
picked up again later, the resumed send used to rely on the permission the
sender had when it first started. Now it checks again, so someone who is no
longer the lead cannot finish sending tasks.

**Agents no longer stop when the shared message store is busy (#154).**

All agents keep their messages in one shared folder. To stop two agents writing
at the same moment, each one briefly takes a "lock" on it: a rule that lets only
one of them write at a time. Before this, an agent that could not get the lock
quickly enough could give up and stop working. Now it waits and retries only the
step that was blocked. It never redoes work it has already finished, and never
sends the same message twice.

What you will notice: during a busy period an agent may show "rate limited or
outage", with the reason "store lock contention", for a short while. Its log
records one note per busy period, and then it carries on by itself. Real
problems are reported as before and are not retried: a damaged file, a
permission error, a lock taken in the wrong order, or an agent that has lost
its claim to its turn.

**Checks recorded on the work board can no longer block the whole team.** The
work board can record automatic checks ("gates") for a single piece of work.
Before this, such a check could be mistaken for a team-wide check and hold up
unrelated work. Now a board check only counts for its own piece of work, and
adding one to the team-wide list is refused. Old entries that were added by
mistake are ignored, with a warning.

##### Technical details

- **New console:**
  - route `/v2`, step M1 of the pitch slice;
  - assets `console2.css`, `console2-model.js` and `console2.js`, under the
    unchanged Content-Security-Policy (system fonts, no inline style or script,
    bus content only through `textContent`);
  - polls `/api/state`, `/api/attention` and `/api/lead-chat`, GET only;
  - `?root=` selects the team, and an unknown one is stated, never replaced;
  - keyboard focus stays in the header;
  - themes: Midnight is styled; Paper, Synthwave and Terminal carry their
    variables;
  - `/` and `/dashboard` are unchanged;
  - plan: `docs/STEP-CONSOLE-V2-PITCH.md`.
- **Health:** new fields `last_known_state`, `last_known_since`,
  `last_known_updated_at` and `last_known_progress_at`. `state: unknown` and all
  existing fields are unchanged. It is used by the v2 console only; status,
  doctor, the supervisor and the classic console are unaffected.
- **Work-board reducer:**
  - a pure function from validated envelopes, placing items by the design's
    precedence rows from explicit correlation only, with a counted legacy group;
  - no bodies, subjects or message-ID order are used;
  - it keeps needs-info HOLDs, follows explicit replacement chains, compares
    whole repository and check policies, and degrades malformed history to a
    per-item Unknown.
- **Shared snapshot:**
  - a per-root worker snapshot over active and compacted envelopes, with
    generation checks and a cross-partition selected-work budget;
  - validated facts are cached across polls.
- **Model vendors:** configured independently of CLI transport. Vendor maps are
  frozen per recipient on dispatches (`assignee_model_vendors`).
- **Feed:** `/api/work-board`, bounded, with exact item, cycle and revision
  checks, and coverage, overflow and legacy groups.
- **Needs-you projection:** shared in `console2-model.js`. Escalation
  `source_refs` carry the validated item and cycle, and attention and lead-chat
  decisions dedupe by escalation ID.
- **Board view:**
  - `console2-board-model.js`, on top of the shared B7 projection;
  - one router owns `#conversation`, `#board` and a reserved
    `#review=<escalation-id>`;
  - fixed, server-authored hash links;
  - keyed cards are reconciled in place.
- **Dashboard race:** the last-known-good view is kept during snapshot scan
  races, with a bounded prompt retry.
- **Broadcast resume:** task authority and reader compatibility are rechecked
  when a partial task broadcast is resumed.
- **Lock contention (#154):**
  - the continuous loop retries only the contended step in place: before
    admission, or after a completed turn for publication (one stable operation
    nonce) and finalization. It never re-drives the turn, including on owed
    (admitted-dispatch) heads;
  - the reason is `rate_limited_or_outage` / `store_lock_contention`;
  - an owner that cannot re-enter a lock's guard removes its own marker on its
    next acquisition or sweep;
  - contention is never persisted as a permanent block, and no gate method
    raises anything new. Failed-delivery disposition, retry-exhaustion
    settlement, block writes and cursor-projection accounting keep their own
    retry, and a contended outcome is left unpersisted;
  - corruption, access denial, unsafe generation changes, lock-order inversions
    and a lost lease keep their existing handling.
- **Gates:** `wb.` gates are isolated from root checks and from both gate-HOLD
  attention paths. New board names in root requirements are refused, and legacy
  contaminated lists produce a warning.

## Sample 2: the description of pull request #246 (the console memory fix)

### Before (exactly as on GitHub)

**Title:** fix(#239): coalesce uncached attention/lead-chat scans causing unbounded serve growth

````markdown
## Mechanism (#239)

`GET /api/attention` and `GET /api/lead-chat` each ran a full, **uncached**, O(store-size) rescan-and-validate of every message file on **every single request** (`web._validated_for_state` / `web._all_messages`). `/api/state` does **not** have this problem - it already reads from the cached `SnapshotService.active(cfg)`.

`ThreadingHTTPServer` caps neither per-endpoint concurrency nor connection lifetime. Once requests for these two endpoints arrived faster than one scan completed, each overlapping request started its **own fully redundant rescan on its own thread**, and each thread retained its own scanned/validated copy of the store until it returned. A real browser (verified with headless Edge against the actual console `/v2` page) fires these polls without waiting for a prior call to finish; a strictly-sequential synthetic client never does - which is why five rounds of synthetic-load testing never reproduced this, and only a real browser did.

Confirmed via `tracemalloc` (25-frame tracebacks) + `threading.enumerate()`/`sys._current_frames()` thread stacks against a real-store copy: every top allocation-growth site rooted at a per-connection handler thread inside `build_attention()`/`build_lead_chat()`'s uncached scan. Reproduced: private bytes climbed ~610 MB/min with handles and threads climbing in lockstep, never plateauing - roughly 20x the reported incident's own rate (~1.9 GB / ~70 min).

## Final design

**Bound concurrency, never share results.** Every caller (`_all_messages`, `_validated_for_state`, and the `/api/attention` route's `coordination_stall`/`supervisor` scans) always runs its own fresh scan **and** its own fresh validation, with its own current config/roster/signing context - nothing is ever cached or shared across callers. What's bounded is *how many such scans may run concurrently per store root*:

- A `threading.Semaphore(2)` per store root (`web._SCAN_CONCURRENCY_LIMIT`). The raw disk scan **and** its validation (roster/kind checks, HMAC verify when enforced) share **one** permit scope (`web._scan_and_validate_bounded`, no nested acquisition) - validation does real per-message work and must count against the same bound as the scan, not escape it once the scan's own permit is released.
- The wait to acquire a slot is itself bounded, not just the concurrency: `web._SCAN_WAIT_TIMEOUT_SECONDS` (3s). On timeout the route answers `503` with `Retry-After: 2` and `{"error": "busy", "retry_after": 2}` (`Cache-Control: no-store` is already sent unconditionally) instead of blocking the handler thread indefinitely - keeping thread/handle count bounded by concurrency plus arrivals within the timeout window, rather than growing with every request that ever had to wait. `do_POST` (the escalation-answer send, the one POST route that can reach a scan) maps the busy signal to the identical 503 JSON `do_GET` does, via a shared `_send_scan_busy` handler helper.
- **`ScanBusy` (the exception the bound raises on timeout) is audited through every broad `except Exception` on every path that can reach a scan helper**, via one shared guard (`web._reraise_busy`, called as the first line of the existing `except Exception as e:` clause) so a saturated bound always reaches the 503 mapping instead of being silently absorbed by a pre-existing errors-as-data/fail-safe catch:

  | path | disposition |
  |---|---|
  | `GET /api/messages`, `GET /api/thread/<rid>`, `GET /messages/<id>`, `GET /api/messages/<id>` | no catch on these paths at all - propagates straight to `do_GET`'s 503 |
  | `GET /api/state` (`_root_state`, per root) | **intentionally does not re-raise** - `/api/state` aggregates multiple roots in one response; a busy root degrades to its own `errors` entry rather than 5xx-ing every sibling root |
  | `GET /api/threads` (`build_threads_index`) | re-raises via `_reraise_busy` |
  | `GET /api/attention` (`_collect_web_attention_items`'s `needs_operator`/`coordination_stall` sources, the stuck-agent enrichment, and the route's own outer catch) | re-raises via `_reraise_busy` at every layer |
  | `GET /api/risk-register` (`build_risk_register`'s stuck-agent inner catch and its outer catch) | re-raises via `_reraise_busy` |
  | `GET /api/lead-chat`, `POST /api/lead-chat` (`build_lead_chat`) | re-raises via `_reraise_busy` |
  | `POST /api/lead-chat` dispatch (`do_POST`) | maps `ScanBusy` to the same 503 JSON as `do_GET`, via `_send_scan_busy` |
  | `GET /api/gates`, `GET /api/ownership`, onboarding sub-block of `/api/risk-register` | not on a scan path at all (no scan helper call) - untouched |

### No sharing: why the single-flight coalescer and the mtime gate were dropped

Two earlier designs tried to **share** one scan's result across concurrent callers instead of bounding how many run at once - both failed review:

- Sharing the whole validated computation (a `_SingleFlight` leader/follower coalescer) let a follower inherit a leader's **stale signing/roster verdict** computed under a different, now-superseded context.
- A follow-up that shared only the raw scan, gated by a publication-generation signal, could still let a follower **miss a message published after the scan it joined had already enumerated the directory** - and the generation signal itself (the messages directory's `st_mtime_ns`) turned out not to be reliable enough on Windows under load (500 sequential publishes produced 406 unchanged consecutive timestamps), so even a corrected generation check could not be trusted.

The shipped design stops sharing entirely: read-after-publication freshness and per-caller validation correctness both hold **by construction** (there is no shared state to be stale), and concurrency is simply bounded rather than unbounded.

## Evidence

- Scan wall time (real store, 19,478 files): median **1.7s warm**, up to **87s on a cold file-system cache**. The cold-cache case is a known limitation of a 3s wait-bound timeout against a truly cold cache (e.g., right after a host restart) - tracked as a fast-follow, not addressed in this PR (see Follow-up below).
- Real headless Edge, default `/v2` view, real-store copy, **10 minutes**, all three signals bounded with no upward trend: private memory 318-369 MB (slope ~0.8 MB/min, flat), handles 186-206 (flat), threads 4-12 (flat). This directly answers the growth this PR set out to fix (private bytes previously climbing ~610 MB/min, handles/threads climbing in lockstep) - and separately answers a growth mode found and fixed mid-PR (bounding concurrency alone left the *wait* unbounded: handles 218->775 / threads 12->122 over 10 minutes before the 503-busy fix; now flat).

## Follow-up (#251)

Serve the same cached `SnapshotService` `/api/state` already reads from for `/api/attention`/`/api/lead-chat` too, instead of even a bounded live rescan - would remove the 3s-wait-vs-87s-cold-scan tension entirely rather than just bounding it. Out of scope here.

## Tests

- `test_concurrent_attention_and_lead_chat_requests_bound_scan_concurrency`, `test_validation_stays_inside_the_scan_concurrency_bound`: peak concurrent scans/validators stay at or below the bound.
- `test_scan_saturation_returns_503_busy_and_thread_count_recovers`, `test_attention_saturation_returns_503_even_on_the_stuck_agent_path`, `test_threads_saturation_returns_503_busy_never_degraded`, `test_risk_register_saturation_returns_503_busy_never_degraded`, `test_lead_chat_get_saturation_returns_503_busy_never_degraded`, `test_lead_chat_answer_post_maps_scan_busy_to_503_not_disconnect`: saturation always answers `503`/`Retry-After`, never a degraded `200`; thread count recovers afterward.
- `test_read_after_publication_is_never_missed`, `test_signing_enforcement_change_is_always_applied_fresh`, `test_roster_change_is_always_applied_fresh`: a call starting after a publication or a signing/roster change always reflects it; a call already in flight keeps its own as-of-call-time answer.
- Two client-side tests in `tests/console2_data.test.mjs` pin the existing (unmodified) client behavior: a 503 is treated like any other failed read (last-good data kept, no special error flash), and the console's pre-existing per-endpoint in-flight guard never overlaps a poll of the same endpoint.
- Full targeted suite (`tests/test_web.py tests/test_lead_chat.py`): 269 passed. `node tests/console2_data.test.mjs`: 50/50 passed. `ruff check`: clean.

## Test plan
- [x] Failing-first regression tests for each design iteration (RED against unfixed/prior-design `web.py`, GREEN after)
- [x] Full targeted Python suite passes (269 passed)
- [x] Client-side data-layer suite passes (50/50)
- [x] Real headless Edge + real-store-copy harness re-run on final code: all three signals (memory/handles/threads) bounded over 10 minutes
- [x] CHANGELOG `[Unreleased] -> ### Fixed` entry reflects the final design
- [x] Confirmation read: GO (all four P2s closed, enumeration table traced and found complete)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
````

### After

**Title:** Stop the console server from using more and more memory while the new console is open (#239)

#### What this changes

The console server is the small program behind the console web page. While the
new console was open in a browser, it kept using more and more memory, about 610
MB more every minute, until the computer slowed down. This change stops that.
Memory now stays flat.

#### Why it happened

Two parts of the console ask the server for information every few seconds:
"what needs your attention" and "the chat with the lead". To answer either
question, the server read through every message the team had ever stored and
checked each one, every time. It kept no saved copy.

A real web browser does not wait for one answer before asking again. When
questions arrived faster than the server could answer them, each new question
started its own complete read of all the messages, side by side with the others.
Each of those reads held its own full copy in memory until it finished. So memory
piled up and never came back down.

Our earlier load tests had missed this, because the test program politely waited
for each answer before asking again. Only a real browser showed the problem: we
watched it with the Edge browser, running without a visible window, opening the
real console page.

#### What we changed

**Only two reads at a time.** Each team now allows at most two of these full
reads at once. They still always read and check everything fresh, so the answer
is never out of date. What is limited is how many run at the same moment.

**Answer "busy" instead of waiting forever.** If a request would have to wait
more than 3 seconds for a turn, the server now answers "busy, please try again in
2 seconds" instead of making it wait. The console already knows how to handle
that: it keeps showing the last good information and asks again. This matters,
because requests that wait forever pile up too. We found that second problem
halfway through this work and fixed it as well.

**The "busy" answer always gets through.** The server has many places that catch
errors and carry on. We checked every one that could receive this new "busy"
signal, so none of them swallows it and shows a misleading half-answer instead.
There is one deliberate exception. The overview page can show several teams at
once, so when one team is busy, it shows a "busy" note for that team only, and
the other teams still appear.

#### Why we did not share one read between requests

Sharing one read between everyone who asks at the same time sounds simpler. We
tried it twice, and both attempts failed review:
- **Sharing the whole checked answer:** a request could reuse a permission check
  that was made under old settings.
- **Sharing only the raw read:** a request could miss a message that arrived just
  after the shared read had started. The signal we relied on to detect new
  messages, the folder's "last changed" time, turned out to be unreliable on
  Windows under load: after 500 quick writes, it had not changed 406 times in a
  row.

So nothing is shared. Every request does its own fresh read, and the only thing
limited is how many happen at once. This way the answers are always correct by
design, not by careful timing.

#### What you will notice

Before this, leaving the new console open made the server's memory climb
steadily, by about 610 MB a minute. The number of open connections and working
threads climbed with it, without stopping.

Now, after ten minutes of the real console in a real browser, against a copy of
a real team's messages, memory stayed between 318 and 369 MB with no upward
trend. Open connections stayed between 186 and 206, and threads between 4 and 12.

You may sometimes see the console keep its last information for a moment when
the server answers "busy". This is expected: it asks again a couple of seconds
later.

#### Known limitation, and what comes next

One full read normally takes about 1.7 seconds. Right after the computer
restarts, when nothing is held in its memory yet, the same read can take up to
87 seconds. In that situation the 3-second wait limit means the console shows
"busy" for a while, until things warm up.

The planned follow-up (#251) will answer these two questions from the same saved
copy that the main overview already uses. That would remove the slow read
entirely, instead of only limiting it. It is not part of this change.

#### How we checked it

- **Bound respected:** automatic checks show that no more than two reads (and
  their checking) ever run at the same time.
- **Every path says "busy" cleanly:** when the limit is reached, every affected
  page answers "busy, try again" rather than a misleading partial answer, and the
  number of threads comes back down afterwards.
- **Always fresh:** a request that starts after a new message, or after a change
  to the team or its security settings, always sees that change.
- **The console side:** two checks confirm that the console treats "busy" like
  any other failed read (it keeps the last good data), and never asks the same
  question twice at once.
- **The full run:** all of the related automatic checks pass: 269 of the
  server's and 50 of the console's. The code style checker is clean.
- **In a real browser:** the ten-minute run described above.
- **Review:** an independent review of the final version agreed with the
  approach, and confirmed that every earlier review comment was resolved.

#### Technical details

- **Cause:**
  - `GET /api/attention` and `GET /api/lead-chat` did an uncached O(store-size)
    rescan and validation per request (`web._validated_for_state`,
    `web._all_messages`);
  - `/api/state` already used the cached `SnapshotService.active(cfg)`;
  - `ThreadingHTTPServer` caps neither per-endpoint concurrency nor connection
    lifetime.
- **Diagnosis:**
  - `tracemalloc` (25-frame tracebacks) and `threading.enumerate()` /
    `sys._current_frames()` against a real-store copy;
  - every top growth site was rooted in a per-connection handler thread inside
    `build_attention()` / `build_lead_chat()`;
  - private bytes grew about 610 MB/min, about 20 times the reported incident
    (about 1.9 GB in about 70 min).
- **The bound:**
  - `threading.Semaphore(2)` per store root (`web._SCAN_CONCURRENCY_LIMIT`);
  - scan and validation share one permit scope (`web._scan_and_validate_bounded`,
    no nested acquisition).
- **The busy answer:**
  - wait bound `web._SCAN_WAIT_TIMEOUT_SECONDS` = 3 s;
  - on timeout, `503` with `Retry-After: 2` and
    `{"error": "busy", "retry_after": 2}`; `Cache-Control: no-store` was already
    sent;
  - `do_POST` maps it through the shared `_send_scan_busy` helper.
- **Error audit:** `ScanBusy` passes through every broad `except Exception` on a
  scan path, via `web._reraise_busy`:
  - no catch on these paths, so it propagates: `GET /api/messages`,
    `/api/thread/<rid>`, `/messages/<id>`, `/api/messages/<id>`;
  - deliberately degrades to a per-root `errors` entry: `GET /api/state`
    (`_root_state`);
  - re-raised: `/api/threads` (`build_threads_index`); `/api/attention`
    (`_collect_web_attention_items`'s `needs_operator` and `coordination_stall`
    sources, the stuck-agent enrichment and the route's outer catch);
    `/api/risk-register` (both catches); `GET` and `POST /api/lead-chat`
    (`build_lead_chat`);
  - `POST /api/lead-chat` dispatch maps to the same 503;
  - untouched, because there is no scan helper on these paths: `/api/gates`,
    `/api/ownership` and the onboarding sub-block.
- **Dropped designs:**
  - the `_SingleFlight` leader/follower coalescer (stale signing or roster
    verdict);
  - a raw-scan share gated by the messages directory's `st_mtime_ns`. On Windows,
    500 sequential publishes gave 406 unchanged consecutive timestamps.
- **Measurements:**
  - real store of 19,478 files;
  - scan median 1.7 s warm, up to 87 s on a cold file-system cache;
  - the mid-PR wait-growth mode: handles 218 to 775 and threads 12 to 122 over
    10 minutes, before the 503 fix.
- **Tests:**
  - `test_concurrent_attention_and_lead_chat_requests_bound_scan_concurrency`,
    `test_validation_stays_inside_the_scan_concurrency_bound`;
  - `test_scan_saturation_returns_503_busy_and_thread_count_recovers`,
    `test_attention_saturation_returns_503_even_on_the_stuck_agent_path`,
    `test_threads_saturation_returns_503_busy_never_degraded`,
    `test_risk_register_saturation_returns_503_busy_never_degraded`,
    `test_lead_chat_get_saturation_returns_503_busy_never_degraded`,
    `test_lead_chat_answer_post_maps_scan_busy_to_503_not_disconnect`;
  - `test_read_after_publication_is_never_missed`,
    `test_signing_enforcement_change_is_always_applied_fresh`,
    `test_roster_change_is_always_applied_fresh`;
  - two client tests in `tests/console2_data.test.mjs`.
- **Results:**
  - `tests/test_web.py` and `tests/test_lead_chat.py`: 269 passed;
  - `node tests/console2_data.test.mjs`: 50 of 50;
  - `ruff check`: clean.
- **Test plan:**
  - failing-first tests for each design iteration;
  - the real-browser harness re-run on the final code;
  - the CHANGELOG `[Unreleased]` `### Fixed` entry reflects the final design;
  - confirmation read: GO, with all four P2s closed and the enumeration table
    traced complete.
