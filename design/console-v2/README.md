# agenttalk Console — handoff v2 (decided direction)

> **In this repository:** a trimmed copy, not the full handoff. The prototype pages need `prototype/support.js`, which is NOT included here, so the "serve the folder" steps below work only with the full handoff zip. The specs in `docs/` stand on their own. See `NOTE-REPO.md` for what was left out and why, and `LICENSE-NOTES.md` for provenance and third-party terms.

**Build this:** *The Conversation* (board **3a**) as the whole console, with four themes (Midnight, Paper, Synthwave, Terminal), a phone layout (**3c**), a qwen budget meter (**3b**), and the rules in **3d**. This replaces the "pick a direction" open question from v1: the dev team's notes settled it (1d is the app, the 1a terminal look is one of its themes, keyboard shortcuts everywhere).

Source repo: `zoolok17/agenttalk` (`master`, v0.91.x). Real console: `src/agenttalk/web.py` (stdlib server, loopback) + `src/agenttalk/web_static/` (vanilla JS, no build step). Keep it that way — no framework, stdlib only, `textContent` for anything that came from the bus.

## Read in this order
1. `docs/01-PRODUCT-CONTEXT.md` — who uses it and how (away for days, checks in by phone, 2 teams on 2 machines, qwen paid per use).
2. `docs/02-DECISIONS.md` — every question and note from the dev team, answered, with the board that shows it.
3. `docs/03-SPEC-APP.md` — the desktop app (3a): layout, every region, every state, exact sizes and colors.
4. `docs/04-SPEC-BUDGET.md` — the qwen budget meter (3b) and its six states.
5. `docs/05-SPEC-PHONE.md` — the phone layout (3c).
6. `docs/06-RULES.md` — name shortening (with reference code), avatars, measured contrast, data we don't have.
7. `docs/07-DATA-CONTRACT.md` — the view model the UI needs, field by field, and where each field comes from in the repo.
8. `docs/08-INTERACTIONS.md` — keyboard map, item lifecycle, offline behaviour, the lead's replies.
9. `docs/09-ACCEPTANCE.md` — checklist to sign the build off.
10. `docs/10-BUILD-PLAN.md` — suggested order, what ships behind flags.

## What's in the zip
- `prototype/` — the interactive design files. **Serve the folder over HTTP** (`cd prototype && python -m http.server`) and open `Console Futures.dc.html`; `file://` won't load `support.js`.
  - `Console Futures.dc.html` — every board. Turn 3 (3a–3d, the decided direction) is at the top; Turns 2 and 1 below are the exploration. Props (Tweaks): `actions` (stands in for `--enable-actions`), `live`, `crt`.
  - `Team Console.dc.html` — the calm baseline console (5 views + lead chat, light/dark). "spec-kitty" and "7/12" are removed from its header.
  - `new_avatars/` (60 shaped avatars, 6 silhouettes) and `avatars/` (10 round, earlier round).
  - `support.js` — runtime for the design files only. Not for production.
- `tokens/themes.json` — the 4 app themes (exact values), runtime badge colors, silhouette per theme, plus the three other directions' palettes.
- `fixtures/turn3.json` — both teams, all three scenarios (busy / quiet / offline), needs items with evidence, budget states, usage windows. `fixtures/turn1-2.json` — earlier mocks (gates, lanes, domains, risks…).
- `reference/name-shortener.js` — runnable reference for the naming rule, with the test cases.
- `reference/TURN-1-2-REFERENCE.md` — full spec of the eight exploration boards (gates, lanes, ownership, risk, tasks, lessons, onboarding are inherited from here).
- `design-system/` — tokens, reference components and guidelines from the calm baseline console.
- `assets/avatars-shaped/contact-sheet.png` — all 60 shaped avatars at a glance.
- `github.md` — repo, branch, and which repo files each board was built from.

No static screenshots are included — open the prototype (boards 3a–3d are at the top of the canvas; click 3a and press `?`).

## Hard rules (from the repo — never break)
1. **Read-only by default.** Waive, deliver, accept risk, restart and "send as operator" only work when the server runs with `--enable-actions`, for the loopback operator, re-checked server-side per request. Otherwise show the control disabled with a CLI hint.
2. **The operator never clicks GO.** Gates decide GO/HOLD from evidence. The operator can only waive a gate or accept a risk, and only with actions on.
3. **Blocking items can't be dismissed.** Only answered, repaired or deferred ("Later"). Deferred items stay countable and recoverable.
4. **Prose never moves state.** Only typed metadata does (review verdict GO/FIX/HOLD, gate state, lane stage).
5. **Cold reviews are evidence, not sign-offs.**
6. **Freshness is always visible.** Every number came from a file some agent wrote. If it's old, it looks old (greyed + "as of"), never live.
7. **Stuck needs evidence.** No Restart without a reason shown next to it ("no output 14 min, tests exited").
8. **Idle is normal.** Grey, never amber.
9. **Bus content is untrusted plain text.** No innerHTML, no markdown.
