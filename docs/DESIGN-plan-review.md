# Design: plan review in the console (#206)

Status: DESIGN ONLY, for a cross-vendor cold read. No production code. Branch `design/plan-review`, base
`master` at `01ddb45`. It builds on:
- console v2: branch `feat/console-v2-pitch`, `docs/STEP-CONSOLE-V2-PITCH.md`; M4 is in progress;
- the challenge skill: PR #205, `feat/challenge-skill`;
- the write path in `src/agenttalk/web.py` on master.

Every claim about existing code below names the function it relies on.

## 0. Summary

1. A plan review is an ordinary lead-to-operator **escalation** carrying `review_*` envelope meta keys. It names a markdown
   file at a commit SHA, the reviewer replies whose findings should show as marks, and the challenge that screened
   the work. The console already turns such an escalation into a needs-you card.
2. The operator opens the card. The document renders from **git objects only**, never the working tree, through a
   DOM-built markdown subset. The reviewers' findings and the challenge verdict are drawn on the same text.
3. The operator adds marks (comment / delete / replace / label), then approves or sends back.
   `POST /api/plan-review` validates every anchor against the file at that SHA. It then publishes **one message**:
   the operator's answer to the escalation (`send_operator_answer_atomic`, which already takes `extra_meta`), with
   the marks in a fenced JSON block.
4. The lead forwards the marks verbatim to the author seat as a `task`. The author replies with one **disposition**
   per mark (applied / declined with reason / superseded), then the lead re-requests review at the new SHA.
5. Reopened, the document shows a **diff since the version you reviewed**. Every earlier mark carries its
   disposition, and reviewer findings are re-anchored by quote or listed as outdated.
6. No new bus kind, no new daemon, no new dependency. There is one new GET route and one new POST route. The POST
   route exists only with `--enable-actions`, behind the existing loopback, Origin, CSRF, rate and 64 KiB guards.
7. Reviewer findings are an optional fenced `agenttalk-findings` JSON block added to today's free-text replies.
   Nothing breaks for a reviewer that does not emit it.
8. Approving a plan is the operator's answer to one escalation. It is not a close GO, and the console still has no
   GO button.
9. Smallest convincing demo: one document, findings from named reviewer replies, four mark kinds, send back,
   per-mark dispositions, diff since reviewed, approve (section 1).
10. Six milestones of about 350-700 lines each, after console v2 M4: backend, frontend and skills seats
    (section 10).

## 1. Scope challenge: the smallest demo that convinces

The claim to make convincing: *the plan you open was already screened and cold-read by independent reviewers of
another vendor; you mark it up like a document; your marks come back answered, one by one, by a team.* The loop
must close on live data. Everything else can wait.

**In the smallest demo (must work end to end):**
- One review-request card. It opens one markdown document at one SHA.
- The challenge verdict (a banner) and the findings of one or two named reviewer replies, drawn as marks.
- Four mark kinds, one verdict (approve / send back), and one typed message back to the lead.
- The author's per-mark dispositions, and the re-request at the new SHA.
- On reopen: the diff since the version the operator reviewed, and each earlier mark with its disposition.

**Cut from the demo, and why:**
- **"Since you approved" diff** (issue item 2, second half). It only matters after a plan is reopened
  post-approval, which the demo never does. It is the same diff machinery with a different base SHA (R5 later).
- **Fuzzy re-anchoring.** An exact, unique quote match re-anchors; anything else is listed as "outdated" with its
  original text. The demo shows both outcomes honestly. Edit-distance matching adds risk and no pitch value.
- **Findings from anywhere on the bus.** Only the reviewer replies named in the request render as marks. A
  bus-wide findings index is a separate feature.
- **Multiple documents per review, mark threads (replies to marks), draft persistence across reloads, CLI
  helpers.** None of these appear in the story.
- **Code diffs and uncommitted diffs.** Already out of scope in the issue. The design keeps "document = markdown
  blob at a SHA", which a code-diff phase can extend later.

**Kept although cheap to cut:** the `label` kind. The stakeholder uses a tool that has it and will look for it;
it costs one enum value and one chip.

**Honest limit of the pitch:** the operator overrode the probe (trying the single-agent tool first). This design
therefore matches that tool's annotate-and-return loop only as far as the issue lists it. It does not chase
feature parity.

## 2. Actors and flow

```
author seat --(design doc committed on a branch)--> lead
lead --challenge (skill #205)--> challenger(s) --verdict--> lead
lead --task: cold read--> reviewer (other vendor) --reply + agenttalk-findings block--> lead
lead --escalate (review meta)--> operator            [console: needs-you card "PLAN REVIEW"]
operator --POST /api/plan-review--> ONE answer message --> lead   [verdict + marks]
lead --task (marks verbatim)--> author --task-response (dispositions, new SHA)--> lead
lead --escalate (review meta, prior review, dispositions)--> operator   [card again: diff + dispositions]
operator --approve--> lead proceeds (dispatch), no close GO implied
```

Workers still reach the operator only through the lead. The operator's marks go to the lead, never straight to a
worker.

## 3. Records and bus transport (question 1)

### 3.1 Review request: lead to operator, an escalation with review meta

The lead sends it with the existing `agenttalk escalate` (a `question` with `needs_operator=true`). When the sender
is the lead-chat lead, `cmd_escalate` already targets the operator principal (`store.lead_chat_identities()`), so
this works whether or not the lead is also the liaison. The review keys are ordinary `--meta` pairs:

| meta key | value | rule |
|---|---|---|
| `review` | `v1` | marks the escalation as a review request |
| `review_doc` | repo-relative POSIX path | section 4 rules |
| `review_sha` | 40-hex commit | must resolve locally, with the path as a blob in it |
| `review_series` | `rs-` + 12 hex | minted by the lead on the first request for a document; reused on every revision |
| `review_prior` | `rv-` id | optional: the operator's previous review in this series |
| `review_dispositions` | message id | optional: the author reply carrying dispositions for `review_prior` |
| `review_findings` | up to 8 message ids, comma-separated | optional: reviewer replies whose findings render |
| `review_challenge` | challenge request id(s), comma-separated | optional: the challenge whose verdict shows as the banner |

Why an escalation and not a new kind:
- It is the existing lead-to-operator channel. The console already builds a card from it
  (`build_attention` → `_answer_action_for_item`) and already has an atomic answer path
  (`send_operator_answer_atomic`).
- The lead's scoped wait on the escalation works unchanged.
- No new kind has to be taught to `recv`, `threads`, the wrapper, `doctor` and every skill.

What changes: `build_attention` projects an additive `review` object from these meta keys onto the item. Every
value is validated against the patterns above and stays envelope-derived: no body ever enters `/api/attention`, a
rule the module already states.

### 3.2 Annotation (mark) record

```json
{
  "id": "m3",
  "kind": "comment | delete | replace | label",
  "anchor": {
    "path": "docs/DESIGN-x.md",
    "sha": "<40-hex>",
    "start": {"line": 42, "col": 0},
    "end": {"line": 44, "col": 17},
    "quote": "exact source text from start to end",
    "prefix": "up to 32 source characters before",
    "suffix": "up to 32 source characters after"
  },
  "text": "the comment, the reason, or the note on a label",
  "replacement": "replace only: the new text",
  "label": "label only: blocker | question | suggestion | nit"
}
```

Anchor coordinates:
- Lines are 1-based. `col` is a 0-based Unicode code-point offset within the line.
- Both refer to the LF-normalised blob text (`\r\n` → `\n`).
- The client computes code points with `Array.from`, so astral characters count once, as in Python.

Survival across re-rendering:
- The anchor is expressed in SOURCE coordinates at a SHA, never in DOM terms. A new render of the same SHA maps it
  back exactly (section 8).
- Across SHAs, `quote` + `prefix` + `suffix` form a text-quote selector for re-anchoring (section 7).

Limits:
- at most 40 marks per review;
- `quote` 1-400 characters, and it must not be only whitespace;
- `text` at most 1000 characters;
- `replacement` 1-1000 characters (an empty replacement is a `delete`);
- ids `m1`..`m40`, unique.

Rules per kind:
- A `comment` needs `text`.
- A `delete` may have `text` (a reason).
- A `replace` needs `replacement`.
- A `label` needs `label`, and `text` is optional.

The server stamps `author` (the operator principal) and `created_at`. The client never supplies them.

### 3.3 Verdict record and the one message that carries it

The client posts:

```json
{"request_id": "esc-…", "verdict": "approve | send_back", "summary": "…", "marks": [...], "nonce": "…"}
```

Rules:
- `send_back` needs at least one mark or a non-empty summary.
- `approve` may carry marks, which then count as non-blocking notes.
- `summary` is at most 2000 characters.

The server publishes exactly **one** message: the operator's answer to that escalation, sent through
`send_operator_answer_atomic(actor=operator, request_id=…, body=…, subject="plan review: <verdict>",
expected_recipient=lead, extra_meta=…)`.

The answer path sets `kind=message`, `request_id`, `operator_answer=true` and `operator_origin` itself. The server
builds `extra_meta`; none of it comes from the client:

```
review=v1  review_id=rv-<12 hex>  review_series=…  review_doc=…  review_sha=…
review_verdict=approve|send_back  review_marks=<n>  review_nonce=<client nonce>
```

The body is written for humans first and for machines second:

```
Plan review: SEND BACK · docs/DESIGN-x.md @ 1a2b3c4 · 3 marks
<summary>

```agenttalk-review
{"schema":"agenttalk-review/1","review_id":"rv-…","doc":{"path":…,"sha":…},"verdict":"send_back",
 "summary":"…","marks":[{…server-stamped…}]}
```
```

One message per review; no fan-out. The escalation's answered state (`resolve_operator_answer_target`) makes a
second submission for the same request fail closed. A double click is harmless: the second POST finds the existing
answer by `request_id`. If that answer carries the same client `nonce` (stored as `review_nonce` in `extra_meta`),
it returns the first result with 200. Otherwise it returns 409 with the existing `review_id`. The web path needs no
new idempotency store; the CLI-side `_operation_idempotency` is not reused.

### 3.4 Dispositions: author to lead, relayed to the operator

The author's `task-response` carries an `agenttalk-dispositions` block:

```json
{"schema":"agenttalk-dispositions/1","review_id":"rv-…","sha":"<new 40-hex>",
 "marks":[{"id":"m1","status":"applied"},{"id":"m2","status":"declined","reason":"…"},
          {"id":"m3","status":"superseded","reason":"section rewritten; see §5"}]}
```

Rules:
- Every mark id of the review appears **exactly once**.
- `declined` and `superseded` need a `reason` (at most 500 characters).
- The lead's next review request points at this reply with `review_dispositions`.
- The console flags any mark with no disposition in red as "not answered". A missing disposition is visible, never
  silently treated as applied.

## 4. Documents (question 2)

Documents come **only from git objects**, read with `git cat-file` at `review_sha`:
- The repository is the git top level of the selected root's store directory (the project whose `.agenttalk/` the
  console watches). Worktrees share its object store, so a commit made in a seat's worktree is readable.
- The working tree is never read. Untracked files, ignored secrets and uncommitted edits cannot be opened.
- A commit that exists only on a remote (not fetched) is reported as "document not available locally (fetch the
  commit)". Nothing is fetched automatically.

Path rules, all checked server-side:
- repo-relative POSIX; no `..` segment, no leading `/`, no drive letter or backslash, no NUL;
- suffix `.md`;
- the path's first segment is in `plan_review.paths` from the store config (default `["docs", "design"]`), so the
  console cannot be turned into a repository browser;
- `git cat-file -t <sha>:<path>` must be `blob`;
- at most 256 KiB and at most 5,000 lines;
- strict UTF-8 (otherwise refused, not replaced).

How a card points to the document: the attention item's additive `review` object (section 3.1). The card shows the
kind **PLAN REVIEW**, the path, a short SHA, and the evidence line ("challenge: proceed · cold read: 3 findings by
X"). Its primary action opens the review view in place (keyboard: `enter`). The console builds no URL from bus data.
The review view is reached through its own client state; the address bar carries at most `#review=<esc-id>`, a
local identifier, not a share link.

How each receiving seat learns a review is waiting:
- **Operator**: the card, and `agenttalk sync` for the liaison (OPERATOR INPUT NEEDED).
- **Lead**: the answer arrives on the escalation thread it opened and is waiting on (scoped wait or
  `--await-reply`).
- **Author**: the lead's `task` with `review_id` meta.

`GET /api/plan-review?root=&request=<esc-id>` serves the view. It is read-only and also works without actions, so a
read-only console can show a plan with its findings; the verdict controls are then locked "actions off". It answers
only for an escalation in this root that carries valid review meta (pending, or answered within the last 50 review
escalations). There is no path parameter at all. It returns:
- the document lines;
- the parsed findings;
- the challenge banner;
- the prior review with its dispositions;
- the diff since the prior review;
- the escalation state (pending / answered, and the answer's verdict).

## 5. Consumption and skill changes (question 3)

Rules for the lead:
- It **does not paraphrase marks away**. It forwards the `agenttalk-review` block verbatim to the author as a
  `task` (`--meta review_id=… --meta review_series=…`), with its own framing above the block.
- It **may decline a mark itself** only by relaying the operator a disposition with a reason. The author seat is not
  the only one allowed to decline.
- On `approve`, the lead proceeds: dispatching work under the normal challenge and review rules. The review answer
  is the operator's decision on that escalation, nothing more.

Rules for the author:
- It applies each mark, commits, and replies with one `task-response`. The reply carries the dispositions block
  covering every mark exactly once, plus `--meta review_sha_next=<sha>`.
- "Applied" means the new SHA contains the change. The console lets the operator check this in the diff.

Skill text changes, universal and project-agnostic, each with both twins and lint invariants:
- **Lead skill:**
  - a short "Ask the operator to review a document" step (when: a major plan or design after its challenge and cold
    read; how: the `escalate` command with the review meta, runnable exactly as written);
  - on the answer: forward verbatim, require dispositions, re-request with `review_prior` and
    `review_dispositions`;
  - never treat an approve as a GO.
- **Listen skill (author side):** a task with `review_id` means applying marks and replying with the dispositions
  block (the exact schema, with an example).
- **Listen skill, review section, and the `review-code` devkit skill (reviewer side):** "when you review a document,
  you MAY add an `agenttalk-findings` block (section 7)". Free text stays the primary reply.
- **Challenge skill:** none. The banner reads the existing challenge thread by its request id.

## 6. Revision diff (question 4)

"Since you reviewed" compares the blob at the prior review's SHA (`review_prior` → that review's `review_sha`) with
the blob at the current `review_sha`, for the same path. A rename starts a new series; the request says so and no
diff is offered.

- The server computes it with `difflib.SequenceMatcher` over the two LF-normalised line lists. It runs no
  `git diff`, so there is no pager, colour, external diff driver or textconv, and no git output to parse.
- It returns structured hunks: `[{op: equal|insert|delete|replace, old_start, old_lines[], new_start,
  new_lines[]}]`. Equal runs longer than 6 lines are collapsed to 3 lines of context on each side.
- Hard limits: both blobs within the section 4 bounds, and at most 400 hunks. Beyond that the view says "too many
  changes to show; open the new version".
- The client renders it with `textContent` only, as a two-gutter view (old and new line numbers). Deletions and
  insertions are marked by class, never by inline style.
- The earlier marks are listed beside the diff, each with its disposition. A mark whose quote falls in a changed
  hunk links to it.

"Since you approved" (cut from the demo, R5 later) is the same computation with the base set to the newest review in
the series whose verdict was `approve`.

## 7. Reviewer findings as marks (question 5)

A reviewer can add this to any reply (`review-result`, `task-response`, `message`), below its free text:

```agenttalk-findings
[{"id":"F1","severity":"major","path":"docs/DESIGN-x.md","sha":"<40-hex>",
  "line_start":42,"line_end":44,"quote":"exact source text, up to 400 chars",
  "text":"the concrete failure scenario, up to 1000 chars"}]
```

- `severity` is one of blocker | major | minor | nit | info. At most 50 findings.
- `quote` is optional but strongly recommended; it is what survives an edit.
- A reply without the block stays exactly as today.
- The block is parsed only for the reply ids named in `review_findings`, and only for findings whose `path` equals
  `review_doc`. Other findings are counted as "N findings elsewhere".
- A malformed block is not an error: the reviewer's card says "findings not machine-readable; see the reply".

Anchoring and staleness:
- `sha` equals `review_sha`: anchored at the line range, and the quote must match inside it (else "anchor
  mismatch", listed, not drawn).
- `sha` is older: the quote is searched in the current blob. An exact, unique match draws the finding as "moved".
  None, or several, lists it as "outdated" with its original line range and quote.
- A finding without a quote cannot re-anchor across SHAs and is listed as outdated.
- Findings are drawn with the reviewer's name and a runtime badge (from the roster `cli`). That is the visible
  "another vendor already read this" signal.

The challenge banner: the challenge verdict meta on the thread named by `review_challenge` (verdict, confidence,
exposed, and the headline, which is the first body line). Several ids combine with the challenge skill's own rule
(assessed verdicts, stop dominates).

## 8. Rendering (question 6)

A pure module, `console2-md.js` (no DOM, no fetch; testable in node like `console2-model.js`), turns the lines into
a block list. Then `console2-review.js` builds DOM nodes with `createElement` and `textContent` only. There is no
`innerHTML` and no library.

Supported subset:
- ATX headings;
- paragraphs;
- `-`, `*` and `1.` list items, with nesting depth by indentation as a class;
- blockquotes;
- fenced code (``` and ~~~), verbatim;
- horizontal rules;
- pipe tables, shown as a monospace preformatted block, not as table markup;
- inline code, bold and emphasis;
- links, rendered as their text only (no `href`, no image).

Anything else renders as plain text.

Line and offset mapping:
- Every rendered text node records its source `(line, col)` start in a `WeakMap`.
- The renderer only ever drops marker characters, so each text node is a contiguous source substring.
- A browser selection's start and end map through that table back to source coordinates.
- The `quote` is the source slice between them. It may include markers, which is fine: the server validates against
  the same source.

Mark display:
- A mark's range is drawn by splitting the affected text nodes into spans classed `mark mark-<kind>`.
- The margin column lists operator marks and reviewer findings, each a button.
- Activating a button scrolls to and outlines its range. Activating a range focuses its button.

Keyboard and focus:
- The review view is a mode of its own. The 2 s poll does not redraw it. A newer revision of the same series shows
  a banner ("a newer version is waiting") instead of replacing the text under the reader.
- Keys:
  - `a` annotates the current selection and opens a small composer with `c`/`d`/`r`/`l` for the kind;
  - `esc` cancels and returns focus to where it was;
  - `ctrl+enter` saves the mark;
  - `m` and `M` move to the next and previous mark;
  - the verdict buttons are reached by `tab`, and a confirmation step is required before submit.
- Focus never jumps on a data refresh, which follows the console v2 reconcile rule.

## 9. Security (question 7)

- **Actions mode only for writes.** `/api/plan-review` joins the POST allowlist in `do_POST` next to `/api/intent`
  and `/api/lead-chat`. Without `--enable-actions` it answers 405, as they do.
- **Same guards as lead-chat:** loopback peer, loopback `Host`, same-origin `Origin`, `Content-Type:
  application/json`, `X-CSRF-Token` compared with `hmac.compare_digest`, the token bucket (`_rate_allowed`),
  `_read_limited_body` (64 KiB).
- **Server-side validation of every mark:**
  - the schema;
  - the kind rules;
  - the limits;
  - path and SHA equal to the escalation's;
  - the blob read at that SHA with the section 4 rules;
  - `quote` must equal the source slice between `start` and `end`, exactly;
  - `prefix` and `suffix` must match the text around it.
  One failing mark rejects the whole submission with its id (422), and nothing is sent.
- **Identity:** the server resolves the escalation with `resolve_operator_answer_target`. It must be pending,
  addressed to the operator, sent by the lead, and carry valid review meta. The operator principal and the lead
  come from `store.lead_chat_identities()`. The client names only the request id.
- **No client-code egress:** the document never leaves the machine. The page loads nothing external (the CSP is
  unchanged). There are no share links and no export. The only persisted copy is the answer message on the local
  bus, which is where marks belong.
- **No working-tree reads, and no path parameter on any route.** A document is reachable only through a review
  escalation.
- **Console v2 lint, and v2's first write:** console v2 is read-only through M3.
  - `tests/test_console2_web.py` bans write requests in every v2 script ("write request (read-only slice)").
  - `test_v2_js_reads_only_the_three_feeds_through_one_fetch` pins `console2.js` to one `fetch` helper and exactly
    `/api/state`, `/api/attention` and `/api/lead-chat`.
  - The composer's send is only a seam (`canAct`); it is not built.
  So plan review is v2's first write path. `console2.js` stays exactly as pinned. A new `console2-review.js` owns
  the review requests, through its own bounded request helper (same timeout and abort rules): `GET /api/plan-review`,
  `GET /api/session` (for the CSRF token), and the single `POST /api/plan-review`. The lint changes deliberately:
  - the write ban stays for every v2 file except `console2-review.js`;
  - that file gets its own pin of exactly those three paths and one POST.
  If the composer slice (`canAct`) lands first, R4 reuses its session helper instead of adding a second one.

## 10. Milestones (question 8)

These start after console v2 M4 merges. Each is cold-readable on its own and comes with its tests; the sizes are
changed lines excluding tests.

| # | What | Size | Seat |
|---|---|---|---|
| R1 | Backend read path: `plan_review.py` (pure; covers meta validation, path rules, bounded `git cat-file` read, findings and dispositions parsers, quote re-anchoring, difflib hunks); the `review` object on attention items; `GET /api/plan-review` | ~650 | backend/bus |
| R2 | Backend write path: `POST /api/plan-review`, mark validation against the blob, answer via `send_operator_answer_atomic(extra_meta=…)`, nonce idempotency, answered-state refusal | ~500 | backend/bus |
| R3 | Frontend read view: `console2-md.js` (pure) plus the review view (open from the card, render, findings marks, challenge banner, margin list, locked verdict when actions are off) | ~700 | frontend |
| R4 | Frontend annotate and submit: selection → anchor, composer (4 kinds), verdict with confirmation, per-mark 422 errors, keyboard; v2's first write (the session/CSRF helper in `console2-review.js`, unless the composer's `canAct` slice already built one) and the one-file lint change | ~650 | frontend |
| R5a | Revision view: diff since reviewed, earlier marks with dispositions, "not answered" flags, moved/outdated findings | ~450 | frontend (+ a small backend delta) |
| R6 | Skills: lead, listen (author and reviewer), and the `review-code` note, both twins; lint invariants; a CLI regression that executes the skill's `escalate` example with review meta, and the dispositions example | ~350 | skills/dev |
| R5b | Later: "since you approved", mark threads, drafts across reloads | ~300 | frontend |

Tests per milestone:
- **R1 and R2:**
  - traversal and allowlist paths, non-`.md`, oversize, non-UTF-8, unknown SHA, remote-only commit;
  - hostile findings (bad JSON, 51 findings, wrong path, lying quote);
  - re-anchor unique / multiple / none;
  - diff bounds;
  - actions-off 405, CSRF, Origin, rate, and body 413;
  - every anchor rejection path;
  - exactly one message per review, double submit, answered escalation.
- **R3 and R4:**
  - a hostile markdown corpus rendered through the recording DOM stub that throws on markup or style injection;
  - an offset table where every rendered character maps back to its source character;
  - selection across inline markers;
  - keyboard and focus with a data refresh in flight.
- **R6:**
  - the skill lint;
  - the executed-command regression, the lesson of PR #205's fix round.

Smallest demo = R1 + R2 + R3 + R4 + R5a + R6, about 3,300 lines plus tests, in that order. R3 can start in
parallel with R2 once R1's JSON contract is fixed.

## 11. The demo on live data (question 9)

This runs on the team's own repository, with a real design document. The console runs with `--enable-actions`
(see open question Q1).

1. **Before the stakeholder arrives:**
   - the author seat commits `docs/DESIGN-<feature>.md` on a branch;
   - the lead runs the challenge (another vendor) and a cold read (another vendor) whose reply carries an
     `agenttalk-findings` block;
   - the lead escalates the review request.
2. **What the stakeholder sees first:** the console's needs-you card. PLAN REVIEW · the path · "challenge: proceed ·
   cold read: 3 findings by <reviewer> (X)".
3. **The operator opens it:**
   - the plan is rendered;
   - the banner shows the challenge verdict and its headline;
   - three reviewer marks sit in the margin with the reviewer's badge. The point to make: *this plan was argued with
     before you saw it.*
4. **The operator marks it up:** replaces a sentence, strikes a paragraph, labels one passage "question", adds a
   comment, then sends it back with a one-line summary. The confirmation shows "4 marks → lead".
5. **In a terminal pane, the bus:**
   - the lead receives one message with the `agenttalk-review` block and forwards it to the author as a task;
   - the author commits a revision and replies with four dispositions (three applied, one declined with a reason);
   - the lead re-requests review.
   This takes real minutes. Cut to it with the terminal visible, or prepare the second revision beforehand and say
   so; do not fake it.
6. **The operator reopens it:**
   - "Since you reviewed" shows the changed lines;
   - each earlier mark shows applied or declined, with the reason;
   - one reviewer finding shows "moved" and one "outdated".
   The operator approves. The card closes and the lead proceeds.

**Not shown:** code or uncommitted-diff review, share links, phone layout, "since you approved", concurrent
annotators, any GO or release control. Approve is never presented as shipping anything. Also not shown: a mark
auto-applied by the console; the author seat applies marks, the console never edits files.

## 12. Open questions for the operator (question 10)

- **Q1. Actions in the demo.** The console v2 pitch decided read-only (its Q10). Through M3, v2 has no write path
  at all. Plan review cannot return marks without `--enable-actions` and without v2's first POST (R2/R4). Run the
  demo with actions on, or show read-only viewing and the return path in the terminal? (Recommended: actions on, on
  a loopback demo machine. That makes the review POST the first write the stakeholder sees, so it should land after
  its cold read, not be rushed for the date.)
- **Q2. What "approve" authorises.** Is the operator's approve permission for the lead to start dispatching the
  plan's work, or input the lead still confirms back in chat? (Recommended: permission for that plan only; never a
  close GO.)
- **Q3. Verbatim or relayed marks.** Should the author see the operator's marks verbatim, attributed to the
  operator, or only the lead's rewrite? (Recommended: verbatim, with the lead's framing above them.)
- **Q4. Which documents.** Only markdown under `docs/` and `design/` of the team's own repository, or also other
  registered repositories (for example a client repository a team works on)? The second means naming those roots in
  the store config, and it widens what the console can read. (Recommended: own repository only for the demo.)
- **Q5. The label set.** A fixed set (blocker, question, suggestion, nit) or operator-defined labels? (Recommended:
  fixed for v1.)

## 13. Risks in this design

- **The text-quote selector can mislead after heavy edits:** a unique quote that moved into a different context
  would re-anchor to the wrong meaning. Mitigation: a re-anchored item is labelled "moved", and the operator sees
  the diff.
- **A dispositions block the author forgets or gets wrong** shows as "not answered". The loop is visible but still
  depends on the skill being followed. The R6 lint and the lead's rule (re-request only with dispositions) are the
  control.
- **Reviewers may not adopt the findings block.** The view then shows the challenge banner and "N reviewer replies,
  not machine-readable". The pitch still works, but weaker. For the demo, the cold-read work order should ask for
  the block explicitly.
- **The 64 KiB body bound caps a review** at roughly 40 full-size marks. This is a deliberate limit, stated in the
  view ("40 marks per review; send back and continue").
