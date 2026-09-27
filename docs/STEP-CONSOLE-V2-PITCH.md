# Console v2 "pitch slice" — step 0 plan

Branch `feat/console-v2-pitch`, base `master` at `fc21190` (v0.92.0). **Plan only, no code.** The build starts
on the lead's go.

Design source: the designer's handoff on branch `docs/console-v2-design` (PR #202, not merged; head `51f28d1`),
under `design/console-v2/`. It is not in this branch's tree. Read it with
`git show origin/docs/console-v2-design:design/console-v2/<file>`. The two pieces the build needs as code
(the name shortener and the Midnight token values) are re-implemented here from the specs, not copied as files,
so this work does not depend on #202 merging. Nothing under `design/` is served, packaged, or edited by this work.

Goal: a live-data slice of board 3a (the desktop app, "The Conversation") in the Midnight theme, with the theme
engine ready for the other three, for a sales pitch. Everything below comes from feeds the console serves today.

## 1. Scope

In: header (no mission progress, no "spec-kitty"); stream (greeting, lead's latest message, needs-you cards with
evidence, deferred line, "also happening", lead chat, composer); rail (usage windows, team roster with shortened
names); quiet-day and offline states; keyboard map and overlay.

Out (later, behind flags, not built here): qwen budget meter and account bar, two machines, phone layout,
Paper / Synthwave / Terminal themes beyond their variable sets, process evidence for stuck agents, the inherited
surfaces (gates, lanes, risk, tasks, lessons, onboarding).

Hard rules carried over (README of the handoff, `web.py` docstring, `LICENSE-NOTES.md` "Rules"): stdlib-only
server; vanilla JS, no build step; bus content only through `textContent`; the existing console CSP unchanged;
read-only by default, writes only with `--enable-actions` for the loopback operator; the operator never gets a GO
button; nothing invents state.

## 2. How v2 coexists with the current console, and how to switch

The current console stays byte-for-byte as it is (`/`, `/dashboard`, `console.css`, `console.js`).

- **New route `GET /v2`** serves a new shell. Same handler shape as `/dashboard` (`render_dashboard`), same
  `_DASHBOARD_CSP`, same `?root=` handling. No other route changes.
- **New allowlisted assets**, three literal keys added to `_STATIC_ASSETS` (exact dict lookup, so the traversal
  guarantee is unchanged): `console2.css`, `console2-model.js`, `console2.js`.
- **Switching**: an operator types `/v2` (or follows a link). Two small links, both plain anchors to fixed paths:
  the classic topbar gets "Try the new console" and the v2 footer gets "Classic view". Preference is not stored
  server-side. `agenttalk dashboard` prints the `/v2` URL next to the existing one.
- **Later, not in this slice**: a `--default-ui v2` flag that decides what `/` serves. Left as an open question
  (Q2) because it changes the default for everyone.
- **Failure isolation**: v2 files are separate. A defect in them cannot break `/dashboard`. The Python change is
  one route and three dict entries.

## 3. CSP and font decision

- **CSP: unchanged.** v2 reuses `_DASHBOARD_CSP` (`script-src 'self'; style-src 'self'; connect-src 'self';
  img-src 'self'`). No inline `<style>`, no `style=` attribute, no inline handler, no external script, font or
  image. The shell carries zero inline anything, like `render_dashboard`.
- **Dynamic widths and colours** (meter fills, the selected-card ring) use CSSOM (`el.style.width = …`,
  `el.style.setProperty('--x', …)`), which `style-src 'self'` allows and which `console.js` already does
  (`console.js:1281`, `:1760`). `setAttribute('style', …)` and `cssText` are banned and tested for.
- **Theme engine**: every theme is a `[data-theme="midnight|paper|synthwave|terminal"]` block of CSS custom
  properties in `console2.css` (keys from `tokens/themes.json`: bg panel panel2 border fg dim accent accentInk ok
  warn bad info serif, plus the runtime colours). Switching sets one attribute on `#app` and writes the choice to
  `localStorage`. Terminal flips `--font-*` to mono and `--radius-*` to 0. Only Midnight is styled and reviewed in
  this slice; the other three blocks exist so the engine is proven, and Paper carries the fixed `dim` / `accent`
  values the handoff recommends (`06-RULES`: `#6B655B`, `#C9481F`), marked "not reviewed".
- **Fonts: a system stack.** `--font-ui: system-ui, "Segoe UI", …; --font-mono: ui-monospace, "Cascadia Code",
  Consolas, …; --font-serif: Georgia-class system serif` (for the greeting and card titles, which the design
  sets in Instrument Serif). No `@font-face`, no remote `url()`. Self-hosting Geist / Instrument Serif / JetBrains
  Mono is a separate change (security review, OFL texts), as the work order says. The layout is sized with
  ch/rem and `min-width: 0`, so a wider or narrower fallback face does not break the 340 px rail.
- **Avatars**: no new binaries. The 71 PNGs already in `web_static/avatars/` include `hexagon-*` (10 files, the
  Midnight silhouette). Assignment is a fixed, explicit role → hexagon map in `console2-model.js` with a stable
  name-hash fallback (the handoff says production should store an explicit mapping; a config-level choice
  through `avatars` prefs already exists and wins when its file is a hexagon). The runtime badge (C / X / Q) is
  CSS. Shaped avatars are drawn with `object-fit: contain`, never circle-cropped.
- **Links**: v2 builds no `href` from bus data. If one is ever needed it goes through a scheme allowlist.

## 4. Architecture

```
src/agenttalk/web.py                     + GET /v2, 3 asset keys, render_console2() shell   (~40 lines)
src/agenttalk/web_static/console2.css    theme variable blocks + layout + components
src/agenttalk/web_static/console2-model.js   PURE: no DOM, no fetch, UMD-style export guard (module.exports)
src/agenttalk/web_static/console2.js     DOM + fetch + keyboard; renders the model; textContent only
tests/test_console2_*.py, tests/console2_*.mjs
```

`console2-model.js` holds everything testable without a browser: name shortener, freshness, stuck-vs-busy,
queue order, age labels, greeting, quiet/offline derivations, roster status lines, usage-window aggregation.
Input: the parsed `/api/state` root, `/api/attention`, `/api/lead-chat`, the local UI state (deferrals, last
visit), and `now`. Output: one plain view-model object shaped like `07-DATA-CONTRACT`. `console2.js` never
derives anything; it renders the model and forwards keys. This is the "view-model adapter" step of the handoff's
build plan.

Polling matches the current console: `/api/state` every 2 s, then `/api/attention` and `/api/lead-chat` per
selected root, one request in flight at a time, `cache: 'no-store'`. Time comes from `generated_at` plus
elapsed local time (the anchor `console.js` already uses), so a skewed laptop clock cannot fake freshness.

Additive server fields are avoided unless a gap forces one (see Q3). `/api/state` stays free of message bodies
(there is a test for that; it stays).

## 5. Milestones (each one cold-readable, target under ~700 changed lines plus tests)

**M1 — Route, shell, theme engine, name shortener.** `/v2`, asset allowlist, shell markup, `console2.css` with the
four theme blocks and the 1240-style grid (`minmax(0,1fr) 340px`, rows `58px 1fr 32px`), header (logo, team
switcher chips, theme segments, `?` button; no progress, no "spec-kitty"), footer key hints, cross-links,
`console2-model.js` with `shortName()` and its full test table. Data-free: renders header and empty regions.
Cold read checks: CSP headers identical to `/dashboard`; no inline anything; allowlist; shortener parity.

**M2 — View model and data layer.** The pure derivations and the polling/fetch layer, rendered as a plain
debug-free skeleton (no styling work). Freshness, offline/quiet derivations, stuck-vs-busy, queue order, age
labels, greeting sentence, roster status lines and state colours, usage-window aggregation, local UI state
(deferrals, last visit, theme). Node tests for every derivation with fixtures adapted from
`fixtures/turn3.json` to the real feed shape. Cold read checks: each derivation against `07-DATA-CONTRACT` and
against the real field it reads (section 6).

**M3 — Stream.** Greeting + sub, lead's latest message, needs-you cards with evidence, deferred line, "also
happening" / "since you last looked" block, lead chat thread, composer, action gating (locked options with
reason, CLI hint when actions are off), answered state. Cold read checks: `textContent` only; no GO control;
answer path is `POST /api/lead-chat` (`answer_escalation` for a pending decision, `lead_chat_send` for free text),
with the CSRF token from `/api/session`, only when that route answers 200.

**M4 — Rail, states, keyboard.** Usage windows, roster with avatars and badges, quiet and offline treatments
(banner, 50 % greying, "as of" stamps, stopped pulse, disabled composer, Retry), keyboard map and overlay,
`prefers-reduced-motion`. Acceptance crosswalk against `09-ACCEPTANCE` for the in-scope rows, with a screenshot
pass on Midnight at 1240×780 and at 1024 wide. Cold read checks: the greying is CSS-class-only; no state is
shown live when stale.

## 6. Element → data mapping, with gaps

Feeds: **S** = `GET /api/state` (roots[]: `agents[]`, `recent[]`, `threads[]`, `counts`, `operator_facing`,
`generated_at`); **A** = `GET /api/attention?root=` (`items[]`); **L** = `GET /api/lead-chat?root=`
(`messages[]`, `available`, `status`, `pending_decisions[]`); **X** = `GET /api/session?root=` (200 only when
`--enable-actions`, else 404). Every agent row carries `health{state, since, updated_at, last_progress_at,
reason_code, age_seconds, stale}`, `last_seen`, `last_seen_age_seconds`, `cli`, `capacity{}`, `task`, `avatar{}`.

| On screen | Comes from | Gap / rule |
|---|---|---|
| Header title "Main team · host · N agents" | S `roots[i].label`, `agents.length` | **G1**: no machine/host name in any feed. Show `label · N agents` unless Q3 is approved (additive `host`). |
| Team switcher chips (one per team) | S `roots[]` (each root = one team); needs count = A count for that root; freshness dot from §7 | Multi-root on one server exists today. Keys 1/2 index `roots`. Only the selected root polls attention/chat, so an unselected chip shows the dot from S alone and its needs count from a low-rate attention poll. |
| Theme segments, `?` | local UI state (`localStorage`) | none |
| Greeting + sub | model: count of open cards, idle count, teams state | wording table from `03-SPEC-APP`; number word for 1–10, digits after |
| Lead's latest message | L `messages[]`: last with `from == lead`, `to == operator` (`body`, `ts`) | No such message → the block is omitted (nothing invented). `available:false` → block shows the liveness detail, greyed. |
| Needs-you cards | A `items[]` | See rows below. |
| card KIND + tone | A `source` / `source_label` | **G2**: server kinds ≠ design kinds. Map: `escalation` → DECISION (info); `gate` → GATE HOLD (warn); client-derived stuck → LOOKS STUCK (bad); `supervisor`, `deadletter`, `coordination_stall`, `other` → their `source_label` in warn (never dropped, per #130). **SPEND LIMIT is not derivable** (no gateway spend backend); a spend question from the lead arrives as an ordinary escalation and shows as DECISION. |
| card title | A `title` | envelope-bounded string, `textContent` |
| card evidence (required) | A `detail` (`why_it_matters`) | **G3**: `detail` can be empty. Then the card shows the age and `source_label` and the line "No evidence recorded" in dim; never a made-up reason (Q5). |
| card age | A `age_seconds` | **G4**: no deadline field exists. Always "no deadline · waiting 2d" (spec branch for "no deadline"). The deadline branch is coded but unreachable. |
| card options (pills) | A `options[]`, present only when actions are on and the item is answerable | **G5**: read-only mode returns no options. Read-only cards show one locked chip "Answer · CLI only" with the CLI hint. First option = primary. |
| card "Later" / deferred line | local UI state (`localStorage`, keyed by item id) | **G6**: the server has no defer write path (intent kinds are send / reply / propose / broadcast / answer_escalation / lead_chat_send). Deferral is per browser, not shared across devices. The deferred count and "show" restore work. |
| Wait 10 min (stuck card) | local snooze (10 min, per item) | same as G6 |
| Restart with context (stuck card) | none | **G7**: no console restart path for a stuck agent. Rendered locked, "Restart · CLI only". Wait stays first, with the handoff's "Weaker evidence: process status isn't visible yet, so Wait comes first." |
| LOOKS STUCK derivation | S agent `health.state`, `health.since`, `health.last_progress_at`, `last_seen_age_seconds`; S `recent[]` | Design rule (`07`): no progress ≥ 10 min, no reply since wake, heartbeat fresh. **G8**: there is no progress *counter* ("+3 in 2 min"); only `last_progress_at`. Wording becomes "Last progress 14 min ago". **G9**: "no reply since wake" is derived from `recent[]` (25 newest envelopes, sender == agent, `ts` > `health.since`); if `since` is older than the oldest kept envelope the model cannot say and does not claim "no reply sent" (the line says "reply status unknown") and does not raise the card. The wrapper's own `stuck_suspected` marks a candidate, never a card by itself (Q4). |
| "Also happening · not for you" | model: agents busy-with-evidence (`working_silent`, progress fresh), capped agents, low-severity A items (`supervisor` low) | wording from the handoff: "dev-5 is quiet, not stuck — last progress 2 min ago · no message for 6m · no card while progress moves". "no message for Nm" from `recent[]`, else omitted. |
| "Since you last looked" (quiet day) | local last-visit timestamp; S `recent[]` filtered by `ts`; S `threads[]` closed verdicts | **G10**: no feed lists "delivered / lesson published / spend delta". Up to 3 lines built only from envelope data: messages since, reviews with verdicts since (`recent.kind` review verdicts), open threads now. The spend line is dropped (out of scope). Fewer than 3 lines is fine; zero lines hides the block. First visit: window = last 24 h, labelled as such (Q6). |
| Lead chat thread | L `messages[]` (`from`, `body`, `ts`) | bodies via `textContent`, no markdown, no links. Own messages right, lead left. Auto-scroll on new message only. |
| Composer | L `available`; X (actions) | Disabled with reason when actions are off ("Read-only: start the console with --enable-actions to message the lead"), when the lead is unavailable (L `detail`), or when offline. Send = `POST /api/lead-chat {body}` with `X-CSRF-Token`. |
| Usage windows (Claude / Codex × 5-hour / weekly) | S `agents[].capacity.primary` (5h) and `.secondary` (weekly): `used_pct`, `resets_at` (epoch s), `confidence`, `observed_at` | **G11**: capacity is per agent, not per runtime. Aggregation rule: per runtime (`cli`) and window, take the reading with the newest `observed_at`; a `stale` / `unknown` confidence renders grey with "as of HH:MM". No reading → the row shows "no reading" (not 0 %). If agents of one runtime disagree by more than 5 points the row shows the freshest and adds "differs across agents" in the tooltip (Q7). Thresholds: < 60 ok, 60–84 warn, ≥ 85 bad. |
| Roster row: avatar, badge | S `agents[].avatar.file`, `cli` | hexagon file per §3; badge letter/colour from `cli` |
| Roster row: short name, tooltip | S `agents[].name` + shortener | tie-break letter needs the team's full id list; project set = root label + directory name + the majority project token of the roster |
| Roster status line + state colour | S `health.state` | See the state table below. |
| Roster right cap ("resets 23:40") | S `capacity.primary.resets_at` when the agent is capped | **G12**: "€ today" (qwen) is not available. Qwen rows show no cap text. |
| Offline / stale | S `generated_at`, agents' `last_seen`, `health.updated_at`, `capacity.observed_at`, `recent[0].ts`; fetch outcome | See section 7. |
| Keyboard, overlay, footer hints | local | none |

Roster state table (design has five states; the health vocabulary has ten):

| `health.state` | Row state | Line |
|---|---|---|
| `working_turn` | working (ok) | "Working · {task} · {age since}" (`task` is envelope-derived; omitted if absent) |
| `working_silent`, progress < 10 min | busy (info) | "Quiet {n}m · last progress {m}m ago" |
| `working_silent` / `working_turn`, stuck rule met | stuck (warn) | "No progress {n}m · reply status …" |
| `idle_waiting` | idle (dim, never amber) | "Idle · {age since}" |
| `rate_limited_or_outage` | capped (bad) | "Capped" + `resets HH:MM` when `capacity.primary.resets_at` is known |
| `unknown` (missing, malformed, or older than the 5-minute TTL) | unknown (dim) | "No fresh health · last seen {age}" |
| `degraded_output`, `errored_poison`, `errored_ambiguous`, `crashed_or_exited` | **down (bad)** — not in the design | "{plain label from the state} · {age since}" (Q4) |

## 7. Freshness, offline, quiet (derivations, exact)

- `source_as_of` = the newest of: every agent's `last_seen`, `health.updated_at`, `capacity.observed_at`, and
  `recent[0].ts`, for that root. Ages use the server anchor.
- **Unreachable**: the last `/api/state` fetch failed, or `generated_at` did not advance for > 3 polls. Banner
  text: "Can't reach the console server" with the time of the last success. Everything greys and freezes at the last
  good snapshot, which stays visible.
- **Silent**: the server answers, but `now − source_as_of > 5 min` for the whole root (the spec threshold; it equals
  the health TTL of 300 s). Banner: "No agent has reported for {age}. Nothing below is live." Same greying.
  These are two different truths (the server is down vs the whole team stopped writing), so the wording differs; the
  design's single "win-ws01 stopped reporting" is right for the second and wrong for the first (Q8).
- Greyed means: `.is-stale` on `#app` → 50 % opacity on data-bearing regions, timestamps rendered "as of HH:MM",
  pulse animation off, bars in `--dim`, composer and action buttons `disabled` with a reason. Last-known data is
  never hidden. "Retry now" triggers an immediate poll; a failed retry shows "Still unreachable · tried HH:MM".
- **Quiet**: zero open cards (after deferrals, snoozes, and answered items) **and** no candidate-stuck agents **and**
  the root is fresh. Greeting "All quiet." + "{n} of {m} agents are idle — that's their resting state; they wake when
  the lead messages them." (n = agents whose state is idle, m = roster size). Idle is grey, never amber.
- Queue order: LOOKS STUCK first, then `age_seconds` descending (oldest waiting first).

## 8. Test plan

Existing tests that must stay green (targeted runs only; never the full suite in this work):
`tests/test_web.py` (`-k "dashboard_shell or csp_split or static_unknown or no_inline or console"`),
`tests/test_runtime_ergonomics.py` (`-k "console_js or render_smoke"`), the client-reference tripwire
(`scripts/client_reference_tripwire.py`), `ruff`, and the package-data check (the three new assets ship in the
wheel by default, verified with the existing wheel-contract check).

New Python tests (`tests/test_console2_web.py`):
- `/v2` → 200, shell has zero `<style>`, `style=`, `on*=`, inline `<script>`, external URL; header CSP string equals
  the `/dashboard` one (byte compare); `Cache-Control: no-store`, `nosniff`, `X-Frame-Options: DENY` present.
- The three new assets are served with the right content types; unknown names and traversal spellings still 404;
  `/dashboard` and `/` unchanged (existing tests plus a byte-identity check of `render_dashboard` output).
- Read-only: `/api/session` still 404 without `--enable-actions`; POST still 405 (unchanged); v2 adds no route
  that accepts writes.
- Source-lint tests over `console2*.js`: no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
  `eval`, `new Function`, `setAttribute('style'`, `cssText`, `setAttribute('href'`/`.href =` from data, `on\w+=`;
  no `http(s)://` literal; no `@font-face`/`url(` in `console2.css`; no "spec-kitty" or "mission" in the shell or
  scripts.
- Token parity: `console2.css` Midnight/Paper/Synthwave/Terminal variables equal an embedded expected table (the
  values in `tokens/themes.json`); if `design/console-v2/tokens/themes.json` exists in the tree the test also
  compares against that file (skipped otherwise, so #202 need not be merged).

New node tests (`tests/console2_model.test.mjs`, run by a pytest wrapper with `skipif(shutil.which("node") is None)`
like `test_runtime_ergonomics.py`; the derivations are pure, so they run without a DOM):
- **Name shortener**: the nine cases of `reference/name-shortener.js` verbatim, plus: the real agent names of the
  live team (round 2 of the handoff says the updated rule shortens all 15; I will take the list from the roster
  and pin each expected short name), `claude-agenttalk-frontend-dev-2`, a project with a hyphen, names that do not match the
  pattern (17 chars + "…", never a middle ellipsis), tie-break only when needed, cross-team prefix.
- **View model**: stuck vs busy (the dev-5 / dev-6 pair from the fixture; busy-silent with recent progress never
  yields a card; stale heartbeat yields "freshness", not "stuck"; `since` older than the recent window → no
  "no reply" claim); queue order; age labels; greeting words and singular/plural; idle count; quiet
  derivation including "deferred items do not count as open"; usage aggregation (newest reading wins, stale grey,
  missing row is "no reading" not 0 %); roster state table for all ten health states; time anchoring with a skewed
  local clock.
- **Offline**: unreachable vs silent, exact thresholds (5:00 / 5:01), recovery clears the banner, last-good data
  retained and marked, "as of" formatting, retry text.
- **textContent-only rendering** (`tests/console2_render.test.mjs`): load `console2.js` against a recording DOM stub
  whose `innerHTML` / `outerHTML` / `insertAdjacentHTML` setters throw and whose `setAttribute('style' | 'href' | 'on*')`
  throws. Render a snapshot in which every bus-derived string (agent names, titles, `detail`, lead body, chat
  bodies, task, reason codes) is `<img src=x onerror=alert(1)>`; assert no throw, every hostile string appears only
  as a `textContent`, and no created element has an `on*` attribute.
- **Keyboard**: keys ignored while typing in the composer; `enter` skips a locked primary option; `l` defers, never
  dismisses; `1`/`2` switch roots; `t` cycles themes; `?`/`esc` overlay.

Manual pass before "ready": a running loopback server on a scratch store with a seeded roster (busy, quiet, and
stale scenarios written as real files, not mocks), Midnight at 1240×780 and 1024 wide, read-only and
`--enable-actions`, screenshots attached to the step record. CI cost: no new dependency; the node tests need
`node` (skipped without it, same as the existing console tests).

## 9. Not built, on purpose

Mission progress/ETA, program stages, signed export, gateway spend (qwen meter, account bar, "€ today", the
SPEND LIMIT kind), team-source list (the multi-root switcher covers same-server teams only), process evidence,
phone layout, inherited surfaces, self-hosted fonts. None of it is stubbed with placeholder numbers.

## 10. Open questions (need the lead's answer before or during M2)

- **Q1 Route name.** `/v2` for the pitch (my default). Alternative `/console`. Any objection to two cross-links in
  the classic console (one line in `console.js`)? That is the only edit to existing static code.
- **Q2 Default UI.** Keep `/` = classic until v2 has phone + budget, then flip with `--default-ui`? (my default: yes,
  not in this slice.)
- **Q3 Host name.** The header wants the machine name and no feed has it. Options: (a) omit it in this slice
  (default), (b) one additive `host` key per root in `/api/state` from `socket.gethostname()` (tiny, needs a
  privacy nod since it shows in the browser and in screenshots).
- **Q4 Stuck and health states beyond the design.** (a) Does the wrapper's `stuck_suspected` count as a stuck
  candidate, or only the handoff's rule? (b) Add a sixth "down" state for `crashed_or_exited` / `errored_*` /
  `degraded_output`, or fold them into "unknown"? I recommend: candidate yes, card only when the handoff's
  evidence holds; "down" as a bad-tone row with the plain reason, no card.
- **Q5 Evidence when the feed has none.** A card whose `detail` is empty: show it with "No evidence recorded"
  (my default, visible and honest), or hide it (violates "never drop", risks losing a human-blocking item)?
- **Q6 "Since you last looked".** Local last-visit timestamp (per browser) and envelope-only lines, fewer than
  the mock's three. OK for the pitch, or should the quiet state show only the idle line?
- **Q7 Usage windows across agents.** One account per runtime assumed; freshest reading wins. If the pitch team
  runs two Claude accounts, one row would misrepresent. Is per-runtime the right grain?
- **Q8 Offline wording.** Two banners (server unreachable / team silent) instead of the design's one. Agree?
- **Q9 Avatars.** Hexagon PNGs already in `web_static/avatars/`, fixed role map. The designer's new 30 avatars
  stay unshipped: authorship is undocumented (`LICENSE-NOTES.md`). Fine for a sales pitch, or wait for the
  licence answer before showing any shaped avatar in the demo?
- **Q10 Actions in the demo.** Should the pitch run with `--enable-actions` (composer and answering live) or
  read-only? Read-only is the safer default and shows locked chips; live answering needs the CSRF session and the
  supervisor kill-switch clear.
- **Q11 Node in CI.** The derivation tests need `node`. If CI runners do not guarantee it, do you want them
  mirrored in Python (the model is ~300 lines of plain logic) or accept the skip?

## 11. M1 record (route, shell, theme engine, header, name shortener)

Lead's answers to Q1-Q11 (2026-09-26): Q1 `/v2` plus the two links; Q2 `/` stays classic; Q3 no host name (a
later label would be an operator-set config value, never `gethostname()`); Q4 stuck candidate + a sixth "down"
state; Q5 show cards with "No evidence recorded"; Q6 per-browser "since you last looked"; Q7 one account per
runtime; Q8 two offline banners; Q9 hexagon avatars already in master; Q10 read-only pitch; Q11 node tests with a
skip. G2 kinds as proposed, G8 wording "last progress N min ago", G9 "reply status unknown" raises no card.

Shipped:

- `GET /v2` (`render_console2()`), same `_DASHBOARD_CSP` object as `/dashboard`; three literal keys in the static
  allowlist; `agenttalk dashboard` prints `console v2 preview at <url>v2`; the classic topbar has a "New console"
  link to the fixed path `/v2`. `/`, `/dashboard`, `console.js` behaviour: unchanged.
- `console2.css`: four `:root[data-theme]` variable blocks (values from `tokens/themes.json`), system font stacks,
  the 340 px rail grid, header and footer. Terminal flips only fonts and radii.
- `console2-model.js` (pure): `shortName`, `parseAgentName`, `teamProject`, theme helpers.
- `console2.js`: header (brand, team chips, theme choice, `?` button), `t` key, footer hint, theme persistence.
- Tests: `tests/test_console2_web.py` (41 pass, 1 skip when `design/` is absent), `tests/console2_model.test.mjs`
  (14), `tests/console2_render.test.mjs` (11), shared `tests/console2_harness.mjs` (runner + a recording DOM stub
  that throws on any markup or style injection).

Changed from the plan above, and why:

1. **Team chips do one `GET /api/state` in M1** (the plan said data-free). The chips need the roots list; the
   polling layer, freshness dot and needs count stay M2. A failed or malformed read shows a disabled "No team
   data" / "Team" chip, nothing more.
2. **Footer hints list only `t theme`.** Advertising `j k enter l / 1 2` before they work would be a false claim;
   they arrive with the keyboard milestone. The `?` button is present and disabled until M4.
3. **`shortName` with an empty `currentProject` keeps the project prefix** (rule 2: "in cross-team lists keep it
   as a prefix"). The handoff's reference code drops nothing in that case; all nine reference cases still pass
   verbatim. The only difference is the previously unspecified empty-context call.
4. **Role words are looked up with an own-property check**, so a role named `constructor` or `toString` is left
   alone (the reference code would return a function).
5. **The classic link is six lines, not one** (element, `href`, `title`, append, plus one CSS rule).
6. **The source lint also bans `method:` and `body:` in the v2 scripts.** It makes "read-only slice" a test: the
   only request is `fetch('/api/state', {cache})`. It must be relaxed deliberately when the composer's send lands.
7. **Paper carries the two contrast fixes the handoff recommends** (`dim #6B655B`, `accent #C9481F`) instead of
   `themes.json`'s values; the token test pins the difference. Not reviewed.
8. **CSS/JS tests strip comments before linting**, so comments can describe what is banned.

Not done in M1 (as planned): the polling data layer, every derivation, stream, rail, states, keyboard overlay.


## 12. M1b and M2 record (focus and ?root= fixes, view model, data layer)

**M1b** (commit `8029417`), from the M1 cold read (Codex, headless Edge): (1) the header redraw dropped keyboard
focus. Controls are now built once and updated in place (theme buttons, team chips); a change in the SET of team
chips rebuilds them and focus returns to the chip with the same key. The recording DOM stub models focus
(`activeElement`, `focus()`, focus lost when a focused node is detached). (2) `?root=` was ignored. It is resolved
like the server does (project_id, then a unique label); unknown, ambiguous or repeated selectors show an explicit
"Unknown team." state with the teams to pick from and never switch silently; picking a team rewrites the address
with `replaceState`. Both regressions fail against the M1 script.

**M2** shipped:

- `console2-model.js` (pure, no DOM/fetch/timers/storage; a test pins that): time helpers, `agentView` (roster
  state, line and colour, stuck evidence), `usageRows`, `freshness` (both offline truths), `buildTeamView`,
  `buildShellView`. The view is plain JSON.
- `console2.js`: polling `/api/state` every 2 s, then `/api/attention` and `/api/lead-chat` for the selected team
  (the other teams' attention on the first round and every 5th). "Now" is `generated_at` plus monotonic elapsed
  time. Last good data is kept on failure. The stream and rail are drawn as plain text (no controls yet) and only
  redrawn when the drawn text changes.
- Node tests: `console2_view.test.mjs` (54), `console2_data.test.mjs` (20), `console2_render.test.mjs` (25),
  `console2_model.test.mjs` (17). Python: `tests/test_console2_web.py`.

Rules as built (each pinned by a test):

- **Stuck card** = health `working_silent` (or the wrapper's `stuck_suspected`) AND no progress for at least 600 s
  (`last_progress_at`, else the turn start) AND heartbeat at most 300 s old AND the recent-envelope window shows no
  message from that agent since it woke. Wording: "Last progress 14m ago · no reply sent · heartbeat still fresh",
  Wait first, Restart locked "CLI only", the weaker-evidence note.
- **Not a card**: recent progress (busy, "no card while progress moves"); a reply since the wake; reply status
  unknown because the 25-envelope window does not reach back to the wake (G9); a stale heartbeat ("not judged
  stuck", a freshness problem); `stuck_suspected` without the evidence (row is busy, "Wrapper suspects a stall").
- **Offline**: unreachable = the last `/api/state` read failed, or `generated_at` did not advance for more than 3
  polls. Silent = the server answers but the newest write (any heartbeat, health, capacity reading or envelope) is
  older than 300 s (5:00 is live, 5:01 silent); no timestamp at all is silent. Unreachable wins over silent. Data
  stays on screen, greyed (`is-stale` on `#app`), roster summary "frozen · as of HH:MM".
- **Queue**: LOOKS STUCK first, then oldest, then id. Kinds: escalation DECISION (info), gate GATE HOLD (warn),
  everything else keeps its `source_label` in warn; the server's own `stuck` items are dropped in favour of the
  evidence rule; known low-severity sources go to "also happening"; an unknown source is always a card.
- **Greeting** modes: busy ("Three things need you."), answered, deferred ("Nothing new needs you."), calm
  (candidates only), quiet ("All quiet." + idle count), offline, needs-unavailable, error, loading.
- **Roster**: lead first; ten health states map to seven row states (`down` and `unknown` added); usage windows
  per runtime with the newest reading winning, grey when stale or already reset, "no reading" never 0 %.

Changed from the plan, and why:

1. **`working_turn` is never a stuck candidate** (the plan table listed it). That state means the wrapper sees the
   turn producing events; `last_progress_at` is only refreshed by explicit progress notes, so an old value on a
   long, healthy turn would raise false cards. Only `working_silent` and `stuck_suspected` can be candidates.
2. **Extra greeting modes** the plan did not enumerate: `deferred`, `calm`, `needs-unavailable`, `error`,
   `loading`, so the page never says "All quiet." when it cannot know.
3. **"Since you last looked" is separate from "Also happening"**: shown in quiet mode whether or not something is
   also happening. Lines are counts of messages / review results / task responses from the 25-envelope window
   ("25+" when the window is all newer than the last visit). The last visit is stored per browser and refreshed on
   load and `pagehide`; it is compared with server time, so a skewed browser clock shifts it.
4. **A failed attention read keeps the last items and marks the page stale** rather than clearing them; a read
   answered for a different team is discarded; an errors-as-data attention answer reads "Can't read what needs you."
   The error text itself is never shown or carried (it can contain paths).
5. **`generated_at` missing** (an older server) falls back to the local clock, so the page still works.
6. **The source lint changed on purpose**: one `fetch(` (in `getJson`), only the three feed paths as literals,
   and the model may not touch `fetch`, `document`, storage or timers. The `method:`/`body:` ban stays for
   `console2.js`, so the page still cannot write.
7. **Tests pin `TZ=UTC`** (the pytest wrapper sets it, and the data test sets it itself) because clock times are
   drawn in the browser's zone.
8. **Avatars are chosen in the model but not drawn yet** (M4): fixed hexagon list, role map, stable hash fallback.

## 13. M2b record (fixes from the M1b + M2 cold read)

- **F1, hung requests.** Every request goes through one `getJson()` with a 5 s timeout (also covers a request
  that never settles, one that ignores its abort signal, and a body that never arrives) and an `AbortController`
  when the browser has one. The poll no longer waits for the feeds: it reads `/api/state`, draws, then starts the
  attention and lead-chat reads on their own, one in flight per feed (a slow feed is not stacked with new
  requests) and each redrawing when it lands. A separate 1 s repaint clock recomputes ages, the silent threshold
  and stale feeds while requests are outstanding. A feed that times out is recorded as failed (attention: "Can't
  read what needs you."; chat: a note), never left as "loading" forever. Tests use requests that NEVER settle,
  not only rejected ones.
- **F2, false LOOKS STUCK from an earlier turn.** Verified against `wrapper/health.py`: `_last_progress_at`
  survives `idle()` and `turn_start()`, and the file carries no turn-start field. What it does carry is `since`,
  the moment the current state began: for `working_silent` that is the turn start (`turn_start()` forces a
  write, and idle to working is a state change), and for `stuck_suspected` it is when the watchdog fired. The
  baseline is therefore `last_progress_at` only when it is not older than `since` (progress inside the current
  state), otherwise `since`. Wording follows: "Last progress N ago", "No progress since the turn began N ago",
  "Wrapper flagged a stall N ago". No backend change is needed for this fix. `tests/test_console2_health_writer.py`
  generates the rows with the real `WrapperHealthWriter` and `Store.read_health` under a fake clock and runs them
  through the node model; it fails against the M2 model.
- **F3, chat feed state hidden.** The model now reads `chat.ok`, `chat.asOfMs` and `payload.available`. The
  newest message stays, with notes next to it: "Lead chat could not be read · last read 1m ago", "Lead chat not
  refreshed for 9s" (a hung refresh), "The lead is unavailable: <detail>" (shown even when an older message
  exists, which `web.py` emits). A failed read with no earlier data also gets its note.
- **F4, attention errors-as-data.** A 200 answer with `errors`, an answer for another team, or a rejected request
  are the same thing: keep the last items, mark the read failed (greeting "Can't read what needs you.", page
  greyed), and let the next good read replace them.

### Finding from the F2 regression (resolved by M2c, section 14): the stuck card could rarely appear with today's health files

`WrapperHealthWriter` writes on state changes and adapter events only; nothing refreshes the file during a
silent turn. `Store.read_health` turns a snapshot older than 300 s into `unknown` and drops its state, `since` and
`last_progress_at` (`health_stale_ttl`). So a genuinely wedged turn reads "No fresh health" after five minutes,
before the ten-minute rule can be evaluated, and the LOOKS STUCK card fires only when health keeps being
refreshed (for example by `agenttalk progress` notes) yet nothing else moves. `test_health_older_than_the_ttl_...`
pins this. Smallest backend addition, if the pitch needs the card on a real wedge: keep the last known snapshot
on the stale read (`health.normalize`, TTL branch: add `last_known: {state, since, last_progress_at,
updated_at}` from the validated raw file; `state` stays `unknown`, `stale` stays true). The console would then
treat "last known state working_silent, no health write for 10 min, heartbeat fresh, no reply" as the stuck
evidence, which is exactly what a silent turn looks like. Not done: it changes a shared reader, so it needs a go.

## 14. M2c record (additive `last_known_*` health fields)

Lead's decision on the section 13 finding: go, additive only.

- **Backend (`agenttalk/health.py`).** `normalize()` has two branches that return `unknown` for a snapshot that
  validated but is too old: older than the TTL (`health_stale_ttl`) and older than the heartbeat by more than the
  skew (`health_older_than_heartbeat`; the TTL check runs first, so this one covers roughly 30 s to 5 min).
  Both now go through `_stale_unknown()`, which returns exactly `unknown(agent, warning)` and then appends
  `last_known_state`, `last_known_since`, `last_known_updated_at` and, when the snapshot had one,
  `last_known_progress_at`. The state is one of the validated `HEALTH_STATES`; the values are the snapshot's own
  timestamps (no free text). `updated_at`, `since` and the rest stay None, `state` stays `unknown`, `stale` stays
  true. The missing, invalid, future-dated and fresh branches are untouched.
  I added `last_known_updated_at` to the three fields the decision named: "health stale N min" cannot be stated
  without knowing when the health was last written.
- **Why the heartbeat branch matters more than the TTL one.** The wrapper writes idle health once, when a turn
  ends, and nothing during a silent turn, while the heartbeat keeps moving. With `/api/state` passing the
  heartbeat, a snapshot goes `unknown` about 30 s after its last write. Before M2c that made every idle agent and
  every silent turn read "No fresh health".
- **Console v2 model.** With `last_known_*` present and a fresh heartbeat (<= 300 s): a remembered
  `working_silent`, `stuck_suspected` or `working_turn` is judged by the same rule as a live one (10 minutes
  without activity, no reply since the wake, fresh heartbeat), with the baseline `since` / progress noted inside
  the turn / (for `working_turn`) the last write. Evidence reads "Health stale 15m (last known: silent turn) · no
  reply sent · heartbeat still fresh"; Wait stays first and Restart stays locked. Before ten minutes the row is
  `busy` with the same "Health stale ..." wording. A remembered `idle_waiting` with a live heartbeat is shown as
  idle (a turn starting would have written at once), so a team of idle agents reads as a quiet team instead of
  `unknown`; this goes one step beyond the decision and is easy to remove (one branch in `agentView`). Every
  other remembered state stays `unknown`, labelled ("last known: crashed or exited (10m ago)"). Without a fresh
  heartbeat nothing is judged. A remembered state outside the known set, or unparseable times, is ignored.
  The line for a candidate with a known absence of replies now says "no reply sent" instead of "reply status
  unknown" (the earlier wording understated what the window shows).
- **Tests.** `tests/test_health_last_known.py` (10): the stale read equals `unknown()` on every existing key
  (values and order), only the four new keys are added and only in the two stale branches, and the readers are
  unchanged: `/api/state`, `/api/status`, `/api/attention`, `/api/lead-chat`, the supervisor report and plan and
  `doctor` produce the same JSON as with the legacy reader (verbatim health dicts compared after stripping the
  four keys; derived outputs compared exactly), under a frozen clock, for both stale branches; the classic
  console does not read the fields. `tests/test_console2_health_writer.py` (8) now reads health through the real
  writer AND the real reader with a heartbeat, as `/api/state` does. `console2_view.test.mjs` +12.

## 15. M3 record (the stream)

Shipped in `console2.js`, `console2-model.js` and `console2.css` (Midnight styling from the theme variables; a test
pins that no colour literal or `rgb()` is used outside the theme blocks):

- Greeting and sub, the lead's latest message (avatar ring with two initials, body, who and when, plus the chat-feed
  notes from M2b), needs-you cards, the deferred line, "also happening · not for you", "since you last looked",
  the lead chat thread and the composer.
- **Cards**: kind (tone colour), age ("no deadline · waiting 5h", or "14m" for a stuck card), serif title, the
  EVIDENCE row (or "No evidence recorded"), the weaker-evidence note, options and "Later". Options are real
  `<button>`s. A locked option is `disabled` with `aria-disabled` and a title that says why ("Answer · CLI only",
  "Restart with context · CLI only"). An option the server served (only with `--enable-actions`) is shown with its
  label, disabled, "· read-only": this slice does not send answers. `canAct` is the seam for the slice that will.
  The only live options are **Wait 10 min** (a per-browser snooze) and **Later**.
- **Later** is a per-browser defer that never dismisses: the card leaves the list, the greeting stops saying "All
  quiet" ("Nothing new needs you. 1 item is deferred: still open, not dismissed."), and a
  "N deferred · still open, not dismissed · show" button brings every deferred card of that team back. The team's
  needs badge counts open cards only. Deferrals and snoozes are stored per team and card id
  (`agenttalk.console2.later`), validated on load (numbers only, at most 500 entries, 30-day expiry, no
  prototype keys), and still work for the session when storage is unavailable. **Wait 10 min** snoozes on the
  server-anchored clock: the card comes back when the ten minutes are over and is listed as "waiting · Snoozed
  until HH:MM" meanwhile.
- **Chat thread**: both sides, yours right and the lead's left, in a bounded scroll box; scrolled to the end on the
  first draw and when a NEW message arrives, otherwise left where the operator scrolled it. The stream also keeps its
  own scroll position across redraws (the stub resets a container to the top when its content is replaced, so this
  is tested against the worst case).
- **Composer**: pinned to the bottom of the stream (`position: sticky`), input and Send disabled, with the reason
  beside it: "Paused — the lead can't receive while the team is offline." (offline), "The lead is unavailable, ..."
  or "Read-only: start the console with --enable-actions to message the lead.".

Found and fixed while writing the card tests: card ids come from a feed, and the model looked deferrals and answers
up with plain property access, so an id such as `__proto__` or `constructor` read as an inherited "truthy" flag and
hid its card. Lookups now use own-property checks, and the stored maps have no prototype (`teamProject` counts too).
Two tests pin it.

Changed from the plan: the composer and the thread live inside `#c2-stream` (no shell change, no second scroll
region); the avatar in the lead block is the two-letter ring, images arrive with the rail work (M4); "show" restores
all deferred cards of the team rather than one at a time (the spec's "click restores"). The M2 "text only" data test
was replaced by `console2_stream.test.mjs`, whose controls test lists exactly which controls may be enabled. The app
harness (`console2_app.mjs`) was extracted from the data test so both suites share it.

Tests: `console2_stream.test.mjs` (25: one per card kind and state, Later/show/persist/reload/per-team/junk
storage, Wait and its expiry, the lead block, the thread and its scrolling, the composer, the enabled-controls
list, hostile text), `console2_view.test.mjs` +8 (thread, composer, per-team state, prototype-key ids),
`test_console2_web.py` +2 (colour literals, sticky composer). A headless-Edge screenshot of a seeded store at
1240x780 (Midnight) was checked by eye; it is not part of the tests.

## 16. M3 fix round record (F1-F4 from the M3 cold read)

- **F1, thread scroll lost.** `chatThread` set `scrollTop` on a detached element; a detached element has no scroll
  layout. The stream is now updated IN PLACE (below), so an existing thread element is kept and never touched by an
  age redraw, and a new or replaced thread is scrolled only after it is in the document, when it is new or has a new
  newest message.
- **F2, focus lost on a routine redraw.** The stream was cleared and rebuilt on every change. It is now built
  off-document and reconciled into the live one: an element whose tag, classes, key and state are unchanged stays
  the same element (only its text changes), so a redraw that only moves an age label keeps focus, scroll and
  selection. Controls carry a `data-c2-focus` key (`team|card|later`, `team|card|wait`, `team|deferred|show`).
  Matching looks ahead, so a note appearing above the cards no longer shifts every block out of alignment, and a
  control only pairs with one of the same key, so a kept button still acts for its own card. If the focused control
  is replaced or its card is gone, focus goes to the same control by key, else to the control now at its position
  among its kind (the next card's Later), else the deferred line, else the first Later, else the stream itself
  (`tabindex=-1`), never the page. Focus outside the stream is never taken. The stream and the rail now have
  separate redraw signatures.
- **F3, a deferral outliving its incident.** A stuck card now carries the turn it is about (`incident.turnStartMs`:
  a silent turn's `since`, also through a stale read's `last_known_since`; unknown for a watchdog flag, whose
  `since` is the flag time). A Later or Wait is void for a turn that began after it was made (a new incident starts
  undeferred, also across sessions), and the local deferral/snooze of `stuck:<agent>` is dropped when that agent
  is verifiably recovered (any state other than stuck or unknown; not judged while the team is offline or
  unreadable). `unknown` keeps the incident open. Residual: a watchdog-only incident that starts while the page is
  closed after an earlier watchdog-only deferral cannot be told apart and stays deferred until the agent is seen
  recovered.
- **F4, misleading composer hint.** It now reads "Messaging the lead is not available in this read-only view. Use the
  classic console or the CLI." The input and Send stay disabled; `canAct` is still the seam.

Tests. The DOM stub now models what a browser does (an element outside the document has no scroll layout and cannot
take focus; a subtree that leaves the document takes focus with it; `removeChild`, `replaceChild`, `insertBefore`),
which reproduces F1 and F2 without changes to the tests' intent. New: 16 stream cases (F1 x3, F2 x8, F3 x6 incl.
the exact recovery sequence, F4), 4 view cases (incident identity, deferral/snooze validity, watchdog residual), and
a real-browser check (`tests/test_console2_browser.py` + `console2_browser_check.mjs`, skipped without node and a
Chromium-family browser; set `AGENTTALK_TEST_BROWSER` to choose one). It serves the real `/v2` shell and scripts
under the console CSP with fixed feeds (30 chat messages, cards that age on every read) and drives headless Edge
over the DevTools protocol. Before the fix it measured `initial scrollTop 0 / scrollHeight 2051 / clientHeight 320`,
`afterRedraw 0`, thread replaced, and `activeElement BODY` after redraws and after Later; after it
`initial 1731`, `afterRedraw 250` on the same element, and focus kept on the same Later button, then on the next
card's Later.

## 17. M4 record (M4a: rail avatars, quiet/offline treatments; keyboard split to M4b)

Split, as the work order allowed: this round is **M4a**. Keyboard map + overlay + `j/k/enter/l/1/2` navigation
and the real-browser keyboard/offline-greying extension are **M4b**, not built here (section 18 tracks it as
open work, not a silent gap). Everything else in the M4 plan (rail avatars and badges, quiet/offline treatments,
`prefers-reduced-motion`, the acceptance crosswalk, screenshots) is done.

### Shipped

- **Avatars.** Roster rows and the lead's message now carry the hexagon avatar (Midnight; the model's existing
  `avatarFile()`, `HEX_MOTIFS`/`ROLE_MOTIF`/stable-hash choice from M2, section 12 item 8) plus the runtime
  badge (bottom-right, `object-fit: contain`, never circle-cropped). `avatarNode()` in `console2.js` is the
  ONLY place an `<img src>` is ever set, gated on membership in `M.AVATAR_FILES` (a closed, ten-entry list the
  model itself produces, cross-checked in a test against the server's own `avatars/*.png` allowlist
  `agenttalk.avatars.AVATAR_ASSETS`) — the value only ever reaches that gate as `avatarFile()`'s return, never
  raw agent text. The "link or source from data" source lint is relaxed by name for exactly this one call site
  (a dedicated test asserts there is exactly one `src` assignment, that it sits inside `avatarNode`, is
  preceded by the allowlist check, and uses the literal `/static/avatars/` prefix; every other href/src/action
  shape stays completely banned). Terminal hides the image and the badge fills the frame (CSS class only, no
  style attribute) — the design's "no image, runtime letter in a box"; Paper and Synthwave still use the
  Midnight hexagon (out of scope: only Midnight is reviewed this pitch, per section 3). The lead's avatar
  carries the full name as its tooltip (06-RULES).
- **Rail reconciled in place — CORRECTED, this was not actually true of M4a as shipped.** This section
  originally claimed `renderRail` used the same off-document-build-then-`syncChildren` discipline as the
  stream (M3 F1/F2). It did not: a patch script used a plain, unchecked string replace for that one edit,
  the replace silently failed to match, and the file was written with the old `clear(rail)` + rebuild-every-
  redraw code unchanged, while every other edit in the same script succeeded and `node --check` and the
  existing tests (which asserted no node identity in the rail) still passed. The M4a cold read (F2) caught
  it — real avatar `<img>` elements were torn down and re-fetched on every ordinary age redraw. Fixed for
  real in the M4a fix round (section 19): `renderRail` now reconciles via `syncChildren`, with `src` in the
  tracked attributes so two different avatar files at the same tree position are never mistaken for the same
  node, and node identity is now pinned by both a node test and the real-browser check.
- **Retry.** Both offline banners now carry a "Retry now" button (`data-c2-focus` keyed, so it keeps focus like
  every other stream control): it stamps `conn.retriedAt` (server-anchored time) and polls `/api/state` at once,
  without waiting for the 2 s cadence. If the team is still not live once that read resolves, the banner adds
  "Still unreachable · tried HH:MM"; a successful read clears `retriedAt`, so a later, unrelated outage never
  shows a stale "tried" time. `freshness()` gained no new parameters; the banner only names a retry when the
  page performed one.
- **Pulse, `prefers-reduced-motion`.** The team chip's freshness dot pulses (2 s ease-in-out, opacity 1→.35)
  only while `is-live`; a `prefers-reduced-motion: reduce` query turns the animation off. It is the only
  animation in the stylesheet.
- **Quiet/offline, otherwise:** confirmed already satisfied from M2/M3 — the banner, `is-stale` 50% greying
  (CSS class only, on `#app`, never a style attribute), the roster summary's "frozen · as of HH:MM", the
  per-window "as of" stamp on a stale usage row, and the composer's disabled state with its reason. Not changed
  in M4a: per-row relative-age text (e.g. "Idle · 40m") kept its wording while offline rather than being
  rewritten to "as of HH:MM". Flagged for the lead in section 18; decided and built in the M4a fix round
  (section 19) — every row's age now freezes at the team's last known-good reading while offline and appends
  "· as of HH:MM", rather than ticking against a clock the data can no longer back.

### Acceptance crosswalk (09-ACCEPTANCE, in-scope rows)

| Row | Status |
|---|---|
| One app, four themes; theme persists per browser | DONE (M1) |
| Terminal changes only fonts and radii | DONE (M1) |
| Header has no mission progress, no "spec-kitty" | DONE (M1) |
| Team switcher: freshness dot + needs count; 1/2 switch | DONE. Dot + count (M1/M2); **click** switches teams (M1b); **key** 1/2 switches teams (M4b, section 20) |
| Every needs card shows evidence; deadline optional; age shown; stuck first then oldest | DONE (M2/M3) |
| Busy-silent agent with recent output never produces a stuck card | DONE, tested |
| "Later" defers; deferred count visible and restorable; nothing dismissable | DONE (M3) |
| Actions requiring `--enable-actions` disabled with a CLI hint when off, re-checked server-side | Disabled + hint DONE; "re-checked server-side" is vacuous here — this build never sends anything (`canAct` is always `false`; no write path exists yet, section 9) |
| Operator has no GO button anywhere | DONE, tested explicitly |
| Quiet day: "All quiet." + idle-is-normal line + since-you-last-looked; idle grey | DONE (M2) |
| Offline: banner, 50% greying, "as of", stopped pulses, disabled composer/actions, Retry; other team unaffected | DONE (M4a), **greying scope revised** (fix round 2, section 22, lead's accessibility decision): last-known TEXT stays fully readable (>=4.5:1); only decorative elements (avatars, usage meters) still grey at 50%/60%. "Other team unaffected" holds for the *silent* truth (judged per team); the *unreachable* truth is inherently the one HTTP connection to the console server, so it is shared by every team on that server by design, not a bug |
| Usage windows: Claude & Codex, 5-hour & weekly, % + reset, threshold colours | DONE (M2) |
| Qwen (flagged) rows/bars | OUT OF SCOPE (needs the gateway-spend backend, section 9) |
| "Raise" answers only message the lead; no limit changes from the console | OUT OF SCOPE (no send capability exists yet in this pitch; the seam (`canAct`, served options) is in place for when it does) |
| Stuck cards without process data use heartbeat/progress/last-message wording, Wait first | DONE (M2, M2c for a stale-but-remembered read) |
| Names follow 06-RULES; full name in tooltip; passes `reference/name-shortener.js` tests | DONE — roster rows already set `title` to the full name (M2); the lead avatar's tooltip was the one gap, closed this round |
| Avatars: silhouette per theme, runtime badge, never circle-cropped | Badge + never-cropped DONE. Silhouette per theme: Midnight DONE; Terminal correctly shows no image (badge fills the frame); Paper/Synthwave use the Midnight hexagon as a placeholder (out of scope: only Midnight is reviewed) |
| Phone (< 1024px) | OUT OF SCOPE (05-SPEC-PHONE, a later card) |
| All text ≥ 4.5:1 | Midnight VERIFIED, including the composited stale state (fix round 2, section 22): a computed WCAG contrast check over every text-token/background-token pair the CSS uses (M4a fix round, section 19), extended to prove the stale-state opacity rules never apply to a text-bearing class. Paper/Synthwave/Terminal remain out of scope (only Midnight is reviewed this pitch, section 3) |
| Bus content rendered as text only | DONE throughout, tested repeatedly (including every M4 avatar/tooltip value) |
| Keyboard map per 08; screen-reader focus order | Keyboard map + overlay, j/k/enter/l/1/2/esc/? DONE (M4b, section 20). Modal focus is now genuinely contained (fix round 3, section 23): the overlay traps Tab/Shift+Tab and the background is `inert`, so focus and Enter-activation can never escape it. Enter is correctly scoped to the navigation context, never diverting a focused control's own native Enter (section 23, R2). j/k now move real focus WITH the selection, onto the card itself, so the highlight and what Enter acts on can never point at two different cards (fix round 4, section 24, R3); closing help restores focus to wherever it was actually invoked from, never a blanket default (section 24, R4). Screen-reader focus order: keyboard focus itself still follows ordinary tab order and every focus-key mechanism from M3; the card *selection* (j/k) is now ALSO real DOM focus (not merely visual), which narrows but does not close the remaining gap - it still carries no `aria-selected`/`role="listbox"` semantics of its own - flagged in section 21 for the lead, not silently claimed done |

### Screenshots (Midnight, live-shaped fixture data: a seeded store, real health/heartbeat files, real `web.serve_in_thread`)

- `atk-scratch/claude-agenttalk-frontend-dev/shots/m4-midnight-1240.png` — 1240×780.
- `atk-scratch/claude-agenttalk-frontend-dev/shots/m4-midnight-1024.png` — 1024×780 (the minimum supported width).

Both show the stuck card with Wait/Restart/Later, the also-happening block, the lead-chat area with its
"lead unavailable" note (the scratch store's config has no resolvable liaison, an honest fallback, not a bug),
the roster with hexagon avatars and runtime badges, and usage windows reading "no reading" (the seeded store
has no capacity data — never a stubbed number). An attempted third screenshot of the silent/offline banner
did not reproduce reliably under headless Chromium's virtual time budget (network I/O does not fast-forward
the way JS timers do) and was dropped rather than kept as a misleading capture; the offline banner and Retry
are covered instead by the automated data-layer tests (`console2_data.test.mjs`), which do not have this
timing problem.

### Tests

Node: `console2_view` +2 (M4: `AVATAR_FILES`/`avatarFile` closed-list checks, the lead's avatar/runtime),
`console2_data` +6 (Retry: immediate poll, "Still unreachable" only after an attempt, no stale label carried
into the next outage, Retry on both banners, focus survives a redraw), `console2_stream` (lead avatar +
tooltip, forbidden-attribute check widened to allow the one sanctioned `<img src>` shape). Python:
`test_v2_js_avatar_src_is_narrowly_gated_and_nothing_else_sets_a_link_or_source`,
`test_v2_avatar_allowlist_matches_the_servers_static_assets`, `test_the_live_dot_pulses_and_...`,
`test_retry_button_is_the_only_control_in_the_offline_banner`,
`test_avatar_css_only_hides_the_image_in_terminal_never_via_a_style_attribute`; the source-lint's per-file
skip list gained one line (documented, not silently loosened). Mutations of the new code (allowlist check
removed, `retriedAt` never cleared/never marked) all go red; the allowlist-removal mutation is additionally
caught by the static lint test even though the (mocked) allowlist itself was bypassed at runtime.

## 18. Open items after M4a (for the lead)

Status as of the M4a fix round (section 19): the rail-reconcile gap (F2), the lead runtime badge (F3), the
Retry/single-flight discipline (F1), the offline literal-timestamps question, and the Midnight contrast pass
are all resolved below. What is still genuinely open:

- **M4b keyboard:** DONE — see section 20. `console2_browser_check.mjs` now also drives the overlay and one
  key path (j then Enter). The offline-banner-mid-run reproduction in the real browser is still not attempted
  (the fixed-fixture stdlib test server would need to flip live to stale mid-run); it stays covered by the
  data-layer tests only (`console2_data.test.mjs`), as section 18 already noted before this round.
- **Paper/Synthwave avatars:** both show the Midnight hexagon today (no separate rounded-square/star assets
  are wired up, since only Midnight is reviewed this pitch). If the pitch demos those themes, say so and I'll
  scope wiring in the existing rounded-square/star files from `web_static/avatars/`.

Resolved this round (see section 19 for detail):

- **Contrast:** DONE. A computed WCAG contrast check now runs against the actual Midnight token values in
  `console2.css` (`test_midnight_text_tokens_meet_wcag_aa_contrast_on_their_backgrounds`), covering every
  text-token/background-token pair the CSS uses. Paper/Synthwave/Terminal remain out of scope this pitch.
- **Offline literal timestamps:** DONE, per the lead's call — every roster row's age now freezes at the
  team's last known-good reading while offline and appends "· as of HH:MM".
- **Rail reconcile (F2), Retry single-flight (F1), lead runtime badge (F3):** DONE — see section 19.

## 19. M4a fix round record (F1-F3, offline ages, contrast)

- **F1, Retry bypassed the single-state-request discipline.** The 2 s poll loop and a manual Retry click each
  called `getJson('/api/state')` independently, with no shared gate; 20 Retry clicks while the server was
  unreachable left 20 requests outstanding, and whichever resolved last — not whichever was newest — won,
  so a stale completion could overwrite a newer one (recovered → stale again), and could apply an
  out-of-order snapshot the same way. Fixed by routing both call sites through one `pollOnce()`, gated on
  `data.statePending` (at most one `/api/state` request ever in flight; a click or a tick that finds one
  outstanding is coalesced, not queued or restarted) plus `data.stateSeq` (an independent belt-and-braces
  check: a completion whose sequence number was superseded before it settled is discarded rather than
  applied, in case that invariant is ever broken by a future change). `retryNow()` no longer issues its own
  fetch; it stamps `conn.retriedAt` and calls `pollOnce()`.
- **F2, the rail was not actually reconciled in place.** See the correction in section 17: M4a's own patch
  script silently failed to land this edit. `renderRail` now builds off-document (`buildRail`) and reconciles
  via `syncChildren`, the same discipline the stream has used since M3 F1/F2, with `src` in the tracked
  attributes so two different avatar files at the same rail position are correctly seen as different nodes.
- **F3, the lead's runtime badge ignored the configured runtime.** `view.lead` resolved runtime/avatar with
  `runtimeOf({name: leadName})` / `avatarFile(leadName, null)` — a name-based guess — even when the lead's own
  roster row (already resolved with the feed's real `cli`) was sitting in `rows`. A `cli: codex` lead with a
  claude-shaped name showed the claude badge on the roster row and the codex badge... nowhere consistent.
  Fixed: the lead's own roster row is looked up by name first; its `runtime`/`avatarFile` are used whenever
  that row exists, and the name-based fallback applies only when no row exists for that name at all (an
  unrecognised or cross-team lead).
- **Offline ages freeze (the lead's call on the section 18 open question).** `buildTeamView` now computes
  `freshness()` before the roster rows, and while offline (either truth: unreachable or silent) every row's
  age is computed against `fresh.sourceAsOfMs` (the newest observed timestamp across the team) instead of the
  live clock, so it freezes rather than keeps ticking against data that stopped arriving; each row's line
  gains "· as of HH:MM" so a frozen reading is never presented as if it were current.
- **Contrast.** A computed WCAG 2 contrast check (`test_midnight_text_tokens_meet_wcag_aa_contrast_on_their_backgrounds`
  in `test_console2_web.py`) parses the Midnight token values directly out of `console2.css` and asserts
  ≥ 4.5:1 for every text-token/background-token pair the stylesheet actually uses (`fg`/`dim`/`serif`/`warn`/
  `bad`/`ok`/`info` as `color`, over `bg`/`panel`/`panel2` as `background`, plus `accent-ink` on `accent`).
  Midnight passes today; the tightest pair is `dim` on `panel2` at 4.69:1.

Tests. `console2_data`: +5 (F1 x3: 20 coalesced Retry clicks, a click racing the loop's own poll, the single
in-flight request always wins), 2 pre-existing tests corrected (one asserted the F2 bug itself as intended
behaviour; the offline time-anchoring test now also covers the freeze, proven by a second offline redraw
producing byte-identical text). `console2_view`: +2 (F3: an explicit `cli`/name disagreement and a generic
seat name; offline freeze: frozen vs ticking, both offline truths, "as of HH:MM"). Real-browser check
(`console2_browser_check.mjs` / `test_console2_browser.py`): the rail's own avatar `<img>` (scoped to
`#c2-rail`, distinct from the lead block's already-reconciled one in the stream) is now also captured and
checked for identity across a redraw the fixture forces by inflating one agent's age every poll. Every new or
corrected test was confirmed red against d351359 (or, for the browser check, against the pre-fix
`console2.js` swapped in) and green against the fix. Full run: `console2_model` 17/17, `console2_render`
25/25, `console2_stream` 42/42, `console2_data` 42/42, `console2_view` 90/90; `test_console2_web.py` 52
passed/3 skipped, `test_console2_browser.py`, `test_console2_health_writer.py`, `test_health_last_known.py`
all green.

## 20. M4b record (keyboard)

Built exactly as planned in sections 5/17-18, entirely in `console2.js` and `console2.css`;
`console2-model.js` is untouched (selection is local UI state, like theme or a deferral, never a
derived model value).

- **The `?` button and overlay.** The header's `?` button (present but honestly disabled since M1) is
  now live: it and the `?` key toggle a keyboard-shortcuts overlay, built once at init and only ever
  toggled by a CSS class (never rebuilt by a data redraw, never a `style=`/`hidden` attribute). Opening
  it moves focus to its Close button; closing it (Close, click outside is NOT wired - only the two
  documented ways, `?`/Escape/Close, close it) returns focus to the `?` button. While it is open, every
  other key is inert (it is modal: `j`/`l`/`t`/etc. do nothing until it closes) - only `Escape` and `?`
  reach it.
- **j/k card selection, with styling.** `j`/`k` move a selection cursor by **card id**, never by DOM
  position, among the currently open cards (clamped at either end, no wraparound; starting from
  nothing, `j` selects the first and `k` the last). The selected card gets `.is-selected`
  (`border`/`box-shadow` from `var(--accent)`, no literal colour) baked into its built className, so an
  unrelated redraw (an age tick) that reproduces the same selection state keeps the same DOM node and
  the same highlight - exactly the existing `sameShape`/`syncChildren` discipline, extended by tracking
  a new `data-c2-card` attribute (`team|id`) so two cards can never be confused with each other by
  position alone (a latent gap the M4a rail-reconcile bug already illustrated for the rail; closed here
  for the stream's cards too). Switching teams, or a card leaving the open list by any means other than
  the keyboard, drops the selection cleanly (never a phantom highlight, never a stale index).
- **Enter opens the selected card.** Focus moves to its first focusable action - `focusables()` (M3)
  already skips disabled controls, so a card whose only options are locked (read-only answers) lands
  on Later, never on a locked option: there is no key that can reach a locked action, by construction.
- **l defers the selected card**, exactly like clicking its Later button; the selection follows to
  whichever card now sits where it did (the next one, else the previous, else nothing).
- **`/` tries to focus the message box.** It stays disabled in this read-only slice, and a disabled
  control cannot take focus - so the key is honestly inert, never an enabled control the page cannot
  back (the same principle as every other locked affordance in this pitch).
- **1/2 switch team by key**, calling the same `pickTeam()` a chip click does.
- **Escape** closes the overlay if it is open, else clears the card selection if one is set.
- All of the above are gated by the existing `isTypingTarget` guard (no key fires while typing in an
  input/textarea/select/contenteditable) and the existing modifier-key guard (`ctrlKey`/`metaKey`/
  `altKey`), unchanged from M1's `t` handler.
- **Footer hints** now also list `j/k`, `l` and `?` (still short; the full map is the overlay's job).

Tests. `console2_render`: the M1 "disabled keys button" test is corrected (the button is no longer a
stub) and the footer-hints test is extended; +4 new (overlay opens/closes and moves focus both ways,
the overlay lists every documented key, other keys are inert while it is open). `console2_stream`: +13
(j/k selection and clamping, k-from-nothing starts at the end, typing-target guard, selection survives
an ordinary redraw with node identity kept, losing the selected card drops it cleanly and `j` after
that starts fresh, l defers and follows the selection, l/Enter no-op with nothing selected, Enter
focuses Later, Enter skips locked options, Escape clears the selection, `/` is inert on the disabled
composer, `1`/`2` switch team by key). Real-browser check (`console2_browser_check.mjs` /
`test_console2_browser.py`): extended to drive the `?` button/overlay open-close-and-refocus cycle and
one key path (`j` then `Enter`) against the real page. Every new/changed test was confirmed red against
the pre-M4b file (`git show 4b16f09:.../console2.js`, and for the browser check, the same file swapped
in on disk) and green against the fix; mutation-tested the streamSig selection signature (drop it -> 5
tests red), the modal key-block (drop it -> 2 tests red), and the locked-option skip in `focusables()`
(drop it -> 2 tests red). Full run: `console2_model` 17/17, `console2_render` 29/29, `console2_stream`
55/55, `console2_data` 42/42, `console2_view` 90/90; `test_console2_web.py` 52 passed/3 skipped;
`test_console2_browser.py` green (overlay + key-path assertions included).

Changed from the plan: the work order said "step doc section 19 as the M4b record" - section 19 was
already taken by the M4a fix round record (written the round immediately before this one, which the
work order's own branch pointer, 4b16f09, is that round's commit); this is recorded as section 20
instead, with the follow-on open-items section renumbered to 21.

## 21. Open items after M4b (for the lead)

- **Card-selection ARIA semantics:** `j`/`k` selection is a visual+behavioral cursor only
  (`.is-selected`), not exposed as `aria-selected`/`role="listbox"`/`role="option"`. A screen-reader
  user gets the same focus-order and control labelling as before M4b, but no independent announcement
  of "card 2 of 5 selected" separate from where browser focus actually is. Not in the work order's
  bullet list; flagging rather than silently deciding it either way.
  - Same offline-banner reproduction gap already noted in section 18: `console2_browser_check.mjs`
  still does not flip the fixture server from live to stale mid-run (it would need a stateful handler,
  not the current fixed-fixture one); that scenario stays covered by `console2_data.test.mjs` only.
- **Paper/Synthwave avatars:** unchanged from section 18 - both still show the Midnight hexagon.

## 22. Fix round 2 record (N1-N3: meter width, offline classification, stale contrast)

The codex delta read of `d351359..4b16f09` (F1-F3) came back clean; this round is two new
regressions and one evidence gap it also found, fixed on top of `ce2df7b` (M4b).

- **N1 (MAJOR), the rail's meter width was never reconciled.** `syncNode` updated only a retained
  leaf's `textContent`; a usage meter's fill has no text at all - its "content" is `fill.style.width`,
  set once in `usageRow` and never touched again by the reconcile. A fresh same-tone reading (41% then
  42%, or a decrease, including a quota reset) kept the OLD bar at the new percentage's label. Fixed by
  giving `syncNode` one narrow, sanctioned case - mirroring `avatarNode` being the only place `src` is
  set - a retained `.c2-meter-fill` node has its CSSOM `width` brought up to date too, never any other
  style property and never through a style *attribute* (still completely banned everywhere else).
  Test: `console2_data.test.mjs` - a same-tone increase then a same-tone decrease, both applied to the
  SAME retained node, alongside an unrelated avatar's identity staying untouched throughout.
- **N2 (MAJOR), freezing the DISPLAY clock while offline had also frozen classification.** The M4a
  fix round's freeze (section 19) was meant to stop each row's printed age from ticking against a
  clock the data can't back - display only. The implementation reused the same frozen `nowMs` for
  every threshold decision in `agentView` too (the 600-second stuck crossing, the heartbeat-freshness
  boundary), and the freeze point (`sourceAsOfMs`, the newest individual *data* timestamp) can sit
  earlier than the instant a row was last correctly judged online (health `updated_at` lags the actual
  poll). Going offline could then recompute a real, unresolved incident's elapsed time against an
  earlier reference and put it back under a threshold it had already crossed - an unresolved stuck
  agent's card would silently vanish the moment the page lost its connection. Fixed by splitting the
  two clocks explicitly in `agentView`: `ctx.nowMs` (possibly frozen) feeds only the printed "Xm ago"
  text; a new `ctx.classifyNowMs` (always the true, live clock, threaded from `buildTeamView`'s own
  `nowMs` parameter) feeds `hbFresh` and the 600-second `quietLong` crossing - the two decisions that
  choose which state, and whether a card exists at all. An outage can now only ever let more true time
  pass (making a real incident's classification more certain, never less); it can never regress one.
  Tests: `console2_view.test.mjs` +2, reproducing the lead's own scenario exactly (`since=-1200s`,
  `progress=-605s`, `updated_at=-10s`, `last_seen=-20s`: stuck while connected, still a stuck card 2s
  after going offline) and the heartbeat-freshness boundary on both sides (still judged at +2s offline,
  correctly reclassified `unknown` at +60s offline, once the heartbeat is genuinely 300s+ old by true
  elapsed time); `console2_data.test.mjs` +1 (an end-to-end DOM check that the default busy-day stuck
  card survives going offline). Two pre-existing tests had accidentally picked timings that landed
  exactly on the heartbeat-freshness boundary (300s, and a cumulative 360s across three redraws) -
  corrected to timings safely under it, since crossing that boundary given enough true elapsed time is
  now correct, intended behaviour, not a bug to pin against.
- **N3 (MINOR), the contrast check didn't cover the actually-rendered stale state; the lead's
  decision.** `test_midnight_text_tokens_meet_wcag_aa_contrast_on_their_backgrounds` (section 19)
  checked flat token pairs, never what the CSS actually paints once `.is-stale` applies: a blanket
  `opacity: .5` over the whole rail and most of the stream (plus `opacity: .6` per stale usage row),
  covering READABLE TEXT along with everything else. Midnight's `--dim` text composited that way over
  `--panel2` measures about 2.1:1 - the design's literal "50% greying" would have failed 4.5:1 for
  ordinary offline roster/usage text, a case the flat-pair check could not see. **Lead's decision,
  recorded here as the accessibility deviation from the design**: last-known TEXT stays fully readable
  during an outage; only decorative elements dim. `console2.css` now applies the stale opacity ONLY to
  `.c2-avatar` and `.c2-meter` (app-wide) and, per stale usage row, only to that row's own `.c2-meter`
  - never to any text-bearing element. The banner and every "as of" label were already, and remain,
  full-strength; staleness is communicated by words (the banner, "as of HH:MM"), never by a colour or
  opacity change alone. Tests: `test_console2_web.py` +2 - one documents the design's original 50%
  opacity would read under 3:1 composited (the N3 finding, generously bounded), the other parses every
  `is-stale` CSS rule that still carries an `opacity` declaration and asserts its selector can only
  ever be `.c2-avatar` or `.c2-meter`, never a text-bearing class (a list of every such class in the
  stream and rail) - a regression here is a compile-time-cheap, CSS-only check, not a rendered-pixel
  one, matching this project's "no headless-Chromium screenshot diffing" testing style throughout.

Red-then-green: every new/changed test confirmed RED against the pre-fix-round file (`git show
ce2df7b:<path>`, swapped onto disk) and GREEN against the fix, for all three findings. Full run:
`console2_model` 17/17, `console2_render` 29/29, `console2_stream` 55/55, `console2_data` 44/44,
`console2_view` 92/92; `test_console2_web.py` 54 passed/3 skipped, `test_console2_browser.py`,
`test_console2_health_writer.py`, `test_health_last_known.py` all green (73 passed/3 skipped total).
`node --check` clean on both touched JS files; `ruff`/`bandit` clean on the touched Python test file.

## 23. Fix round 3 record (N3 narrowed, R1 modal focus, R2 native Enter)

The codex read of `4b16f09..debaaac` (M4b + fix round 2) closed N1 and N2 clean; this round is one
narrowed finding on N3 and two new MAJOR findings from testing with real, native input in Edge -
fixed on top of `debaaac`.

- **N3 narrowed, the avatar fix round 2 shipped still dimmed the runtime badge.** `.is-stale
  .c2-avatar { opacity: .5 }` (section 22) dims the WHOLE avatar box, including the
  `.c2-avatar-badge` runtime letter nested inside it - a text-bearing descendant a selector-substring
  check can't see, since the selector text never mentions "badge" at all; CSS opacity composites the
  whole subtree regardless. Real Edge measured 2.60/2.82/2.47:1 for C/X/Q once dimmed. Fixed by
  narrowing the selector to `.c2-avatar-img` only (never the box), so the badge - informational
  whenever a seat's configured runtime differs from its name - stays full-strength even while stale.
  The section-22 blocklist-style CSS test is replaced with an ALLOWLIST of exact selectors (every
  `is-stale` rule that still carries `opacity` must target one of exactly `.is-stale .c2-avatar-img`,
  `.is-stale .c2-meter`, `.c2-usage-row.is-stale .c2-meter`), since a blocklist of class-name
  substrings cannot see a container rule dimming a nested descendant it never names. Tests:
  `test_console2_web.py` +3 (the allowlist check itself; badge-text-on-fill contrast for all four
  runtime colours at full opacity; a documentation check that the OLD whole-avatar dimming would have
  failed contrast for the badge too, mirroring section 22's own composited-would-fail check).
- **R1 (MAJOR), the keyboard overlay did not contain focus or make the background inert.** `onKey`
  returned early for every key but `Escape`/`?` while the overlay was open - which stops nothing:
  returning early from a JS listener does not cancel the browser's OWN default action for a key it
  never called `preventDefault()` on. With native key events in real Edge: Shift+Tab left the dialog
  entirely and reached the "Classic view" link beneath it, and Enter then navigated the page to
  `/dashboard` while the overlay still believed itself open. **This is precisely the gap a
  `dispatchEvent`-based check cannot see**: a synthetic `KeyboardEvent` only ever fires JS listeners,
  never the browser's native Tab-focus-cycling or Enter-activates-a-focused-control defaults, so
  M4b's own overlay tests (all `dispatchEvent`) passed throughout despite this. Fixed two ways: (1)
  every background region (header, stream, rail, footer) is made genuinely `inert` (never a style
  attribute - a plain boolean attribute, like `disabled`) while the overlay is open, so Tab and a
  click are refused by the browser itself, not merely discouraged by JS; (2) `onKey` also explicitly
  traps Tab/Shift+Tab on the overlay's own focusable set (today just Close) as a second, explicit line
  of defence - the only one the node DOM stub can exercise, since it does not implement `inert`
  semantics. Tests: `console2_render.test.mjs` +2 (inert toggles on open/close; Tab and Shift+Tab both
  land back on Close, with `preventDefault` confirmed called). Real-browser check
  (`console2_browser_check.mjs`/`test_console2_browser.py`), rewritten to drive Tab, Shift+Tab, Enter
  and Escape with genuine CDP `Input.dispatchKeyEvent` calls (a `pressKey` helper) instead of
  `dispatchEvent` - confirms native Tab/Shift+Tab both land on Close, the background is `inert` while
  open, a native Enter on Close closes the dialog without navigating anywhere (`location.pathname`
  unchanged) and un-inerts the background, and a native Escape still works too.
- **R2 (MAJOR), Enter on a selected card stole native activation from real controls.** The Enter
  handler fired whenever a card was selected, with no regard for what actually had focus: a second
  Enter after the first had already moved focus onto the card's own Later button re-triggered "open
  the selection" instead of letting the browser's native Enter-activates-a-focused-button default
  click it - so two Enters, expected to defer a card, left it undeferred. The same unconditional
  `preventDefault` could equally swallow Enter on a focused theme, team-chip or Retry button. Fixed by
  scoping the shortcut to the navigation context: Enter only opens the selected card when the
  currently focused element is not itself a native interactive control (button, link, input, select,
  textarea) - exactly the case after `j`/`k`, which never call `.focus()` on anything themselves.
  Whenever focus already sits on a real control, Enter is left alone and the browser's own default
  activates it. Tests: `console2_stream.test.mjs` +1 (a native-shaped Enter dispatch, target set to
  the now-focused Later button, must NOT be intercepted - `preventDefault` not called, focus
  untouched). Real-browser check: with genuine CDP key events, a second native Enter on the focused
  Later button now defers the card (two cards -> one), and a native Enter on a focused theme button
  (with a card still selected) changes the theme and keeps focus there, never diverted back to the
  selection shortcut.

A note on the CDP mechanics: getting a native Enter to actually activate a focused `<button>` over
`Input.dispatchKeyEvent` needed `type: 'keyDown'` (not `'rawKeyDown'`) plus a `text`/`unmodifiedText`
of `'\r'` on the event - without it, Chromium accepted the event (listeners fired) but never ran the
control's own default action, which would have silently made the R1/R2 real-browser checks pass for
the wrong reason (nothing native ever actually fired). Confirmed by first reproducing that exact false
pass, then fixing the dispatch and reproducing the true regression before applying either code fix.

Red-then-green: every new/changed test confirmed RED against the pre-fix-round file (`git show
debaaac:<path>`, swapped onto disk for the node suites; the same file swapped in for the real-browser
check) and GREEN against the fix. Mutation-tested `setBackgroundInert` and the Tab-trap branch
together (both disabled -> the real-browser check's Tab/inert assertions correctly failed) and
`isInteractiveTarget` alone (short-circuited to `false` -> the R2 node test correctly failed). Full
run: `console2_model` 17/17, `console2_render` 31/31, `console2_stream` 56/56, `console2_data` 44/44,
`console2_view` 92/92; `test_console2_web.py` 56 passed/3 skipped, `test_console2_browser.py`
(rewritten, native-input, green), `test_console2_health_writer.py`, `test_health_last_known.py` all
green (75 passed/3 skipped total). `node --check` clean on both touched JS files; `ruff`/`bandit`
clean on the touched Python test files.

## 24. Fix round 4 record (R3 j/k focus, R4 help focus restore)

The codex delta read closed N3, R1 and R2 with native-key evidence and found two more, both about
where real DOM focus actually sits - fixed on top of `4865d40`.

- **R3 (MAJOR), j/k moved the visual selection but left real focus behind on a stale control.**
  `moveSelection` only ever toggled the `is-selected` class; it never touched
  `document.activeElement`. R2's own fix (fix round 3) correctly lets a focused interactive control
  keep its native Enter - which is exactly what made this visible: once Enter had focused card A's
  Later button, a second `j` visually moved the highlight to card B while focus stayed on A's Later,
  so the next Enter activated A (deferring the wrong card) instead of opening B. Fixed by making
  each card itself focusable (`tabindex="-1"`, plus a `data-c2-focus` key with role `card` - the same
  convention every other stream control already uses, so `captureFocus`/`restoreFocus` keep it in
  place across an unrelated redraw exactly like a button) and having `moveSelection` focus the newly
  selected card directly. Enter still "opens" a card by drilling into its first control (unchanged);
  what changed is that `j`/`k` now first return focus to the card itself, so the highlight and real
  focus can never point at two different cards. A `.c2-card:focus-visible` outline (accent-coloured,
  matching every other control) was added so the browser's own focus ring is visible on a card,
  pairing with - not fighting - the `is-selected` border.
- **R4 (MINOR), closing help always focused the header `?` button, never wherever help was invoked
  from.** `setOverlayOpen(false)` unconditionally focused `chrome.keysBtn`. Opening help with the `?`
  *key* (not a click on the button) while, say, a Later button was focused, then closing it, lost the
  operator's place - and fed directly into how R3 was reproduced. Fixed by capturing
  `document.activeElement` as `nav.overlayInvoker` at the moment the overlay opens - BEFORE the
  background goes `inert`, since an inert ancestor forces its focused descendant to blur - and
  restoring focus to it on close if it is still connected (`isConnected`), else falling back to the
  `?` button deliberately (the same fallback as before, now only used when there truly is nowhere
  better to go, e.g. the invoker's card was itself removed while help was open).

Tests, both in the real browser with genuine CDP key events (`console2_browser_check.mjs`), per the
work order:
- R3: the fixture's two escalation cards (oldest first: card-2 then card-1) are used to reproduce
  the exact scenario - `j` selects card-2 (A), `Enter` focuses A's Later, a second `j` must move
  focus onto card-1 (B) itself (checked by `data-c2-card` identity, not merely a class), `Enter`
  opens B, and a second native `Enter` must defer B specifically - checked by asserting the
  *remaining* card is still A's own `data-c2-card` key, not just a card count.
- R4: a Later button is focused, `?` opens help (focus moves to Close, as before), a native `Escape`
  closes it - focus must land back on that same Later button (`isConnected` too), never the `?`
  button.

Both are also pinned at the node level (`console2_render.test.mjs`, `console2_stream.test.mjs`),
though the node DOM stub cannot show `inert`-driven blur, only that the invoker-capture/restore logic
itself is wired correctly. Two pre-existing overlay tests in `console2_render.test.mjs` were also
updated: one to explicitly focus the `?` button before clicking it (`Element.click()` does not itself
focus its target the way a real pointer click does - neither in this DOM stub nor, it turns out, over
CDP; the real-browser check needed the same explicit `.focus()` before every programmatic `.click()`
on that button, once R4 started asking "who really invoked this"), and one whose old expectation
("Escape always returns focus to the `?` button") was precisely the R4 bug and is now corrected to
the genuinely-invoked-from element (`document.body`, in that specific test, since nothing had focus
before the `?` key was pressed there).

Red-then-green: every new/changed test confirmed RED against the pre-fix-round file (`git show
4865d40:<path>`, swapped onto disk for the node suites and for the real-browser check) and GREEN
against the fix; the real-browser R3 check usefully failed with a thrown exception under the old code
(indexing a card that no longer existed, because the wrong one had been deferred) rather than a clean
assertion - still valid, unambiguous red. Mutation-tested `moveSelection`'s focus call (disabled -> 2
R3 tests red) and R4's invoker-restore fallback (short-circuited to always use `chrome.keysBtn` -> 1
R4 node test and 1 pre-existing overlay test both correctly went red). Full run: `console2_model`
17/17, `console2_render` 31/31, `console2_stream` 59/59, `console2_data` 44/44, `console2_view` 92/92;
`test_console2_web.py` 56 passed/3 skipped, `test_console2_browser.py` (extended for R3/R4, green),
`test_console2_health_writer.py`, `test_health_last_known.py` all green (75 passed/3 skipped total).
`node --check` clean; `ruff`/`bandit` clean on the touched Python test file.

## 25. Final cross-vendor sweep record (F1 clock re-anchor, F2 stale all-clear, F3 expired capacity)

A fresh codex reader swept the whole branch (`fc21190..7a40b57`) as one change (not a delta read) and
found no issues in GET-only, `textContent`, the asset allowlist, CSP reuse, disabled actions or health
compatibility - three findings against the model/data layer, fixed on top of `7a40b57`.

- **F1 (MAJOR), a repeated `generated_at` re-anchored the clock on every successful read.**
  `ingestState` unconditionally set `data.anchor = { epochMs: gen, perf: perfNow() }` on every
  successful HTTP response, even when the payload's `generated_at` was identical to (or older than)
  the one already held - correctly counted as a stalled poll (`stalledPolls` incremented, eventually
  the unreachable banner), but the SAME response also reset the elapsed-time anchor back to that same
  `gen`, with a freshly-read `perf`. Net effect: while a server kept returning one unchanging
  snapshot, `nowMs()` never advanced past that snapshot's own timestamp, no matter how much real time
  passed - ages froze at "0s ago", and a stuck agent's heartbeat could never age past its
  freshness boundary, so a card that should have gone from "stuck" to "unknown, heartbeat stale" (per
  fix round 2's own N2 fix - see section 22) never did, because the classification clock it depends
  on was itself being rewound. The third distinct time-anchoring defect in this console (after the
  M4a freeze and the fix-round-2 classification split); the work order asked to pin it with a test
  that holds the snapshot constant while the clock moves - done. Fixed: the anchor (and
  `data.generatedMs`, and `data.conn.lastOkMs`) now only ever move forward, on a payload whose
  `generated_at` is genuinely newer (or on the very first read). A stalled feed is detected
  (`stalledPolls`) without also rejuvenating the timestamps that detection depends on.
- **F2 (MINOR), a stale attention cache could still claim an unqualified all-clear.** `buildTeamView`
  already computed `view.needs.stale` correctly (an aged or failed attention read), but the
  mode/greeting selection never consulted it: with nothing currently OPEN (no cards, no health-based
  candidate, nothing deferred/answered), it fell straight to the `quiet` mode's "All quiet. Nothing
  needs you." - even when that "nothing" came from a cache that had not refreshed in time (e.g. a
  team switch whose new attention request was still pending past `ATTENTION_FRESH_S` = 8s). Fixed
  with a new mode, `needs-stale`, inserted ahead of the `quiet` fallback (but after the genuinely
  affirmative `busy`/`answered`/`deferred`/`calm` states, which rest on real cached or local data, not
  on an assumption that nothing new exists): "Can't confirm nothing needs you. The attention read has
  not refreshed for Xs; showing the last known result, not a current all-clear." Reproduced and
  tested both as a direct model call and, per the work order, through the ordinary polling schedule
  (switching teams with the new team's attention request left genuinely pending, not merely a
  fixture).
- **F3 (MINOR), expired capacity was used as a current quota diagnosis.** `cappedLine` read
  `used_pct >= 100` off the cached capacity reading with no regard for `confidence` or whether the
  window's own `resets_at` had already passed - so a fresh health read of `rate_limited_or_outage`
  next to a stale, long-expired 100%-used cache still produced "5-hour window full" / "is capped",
  actively misreporting a present provider outage or live rate limit as a stale quota claim (while the
  usage panel elsewhere, correctly, already said "reset passed"). Fixed by requiring BOTH
  `confidence === 'fresh'` AND an unexpired (or absent) `resets_at` before a specific window claim is
  made - the exact same freshness test `usageRows` already applies to the rail's own usage bars, now
  shared by the roster's capped diagnosis too. Expiry is judged against the true clock
  (`classifyNowMs`, threaded the same way N2 established in fix round 2), not a display freeze, so an
  outage cannot rejuvenate stale quota evidence either. Falls back to the honest, coarse "Rate limited
  or provider outage" whenever the specific claim cannot be backed.

Tests: `console2_data.test.mjs` +2 (F1: a fixed `generated_at` while the clock advances six minutes;
F2: the ordinary-polling-schedule team-switch reproduction), `console2_view.test.mjs` +2 (F2: a
stale, empty attention cache against an idle-only roster, with a fresh control case; F3: stale/expired
capacity against three cases - expired, current, and fresh-but-past-its-own-reset). Every new test
confirmed RED against the pre-sweep file (`git show 7a40b57:<path>`, swapped onto disk) and GREEN
against the fix. Mutation-tested each fix directly: F1's `advanced` check (short-circuited to always
true -> 2 tests red, including a pre-existing one), F2's `needs-stale` branch condition
(short-circuited to false -> both new tests red), F3's `current()` freshness check (short-circuited to
always true -> the new test red). Full run: `console2_model` 17/17, `console2_render` 31/31,
`console2_stream` 59/59, `console2_data` 46/46, `console2_view` 94/94; `test_console2_web.py` 56
passed/3 skipped, `test_console2_browser.py`, `test_console2_health_writer.py`,
`test_health_last_known.py` all green (75 passed/3 skipped total). `node --check` clean on both
touched JS files.
