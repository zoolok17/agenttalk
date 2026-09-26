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
