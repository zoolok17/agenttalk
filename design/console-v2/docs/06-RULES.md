# 06 · Rules

## Names
Pattern: `<runtime>-<project>-<role>[-<n>]`, runtime ∈ claude | codex | qwen.
1. **Drop the runtime** — it's shown by the avatar badge (C / X / Q).
2. **Drop the project** when it matches the team on screen. In cross-team lists keep it as a prefix: `shopfront/dev-1`.
3. **Shorten the role, keep the number**: developer→dev, reviewer→rev, tester→test, architect→arch, lead→lead.
4. **Break ties** with the lowercase runtime letter, only when needed: `c.dev-1` / `x.dev-1`.
5. Names that don't fit the pattern: show in full, cut at the **end** at 17 chars + "…". Never middle-ellipsis.
6. Full name always in tooltip / long-press and in every detail view.

| Full | Context | Short |
|---|---|---|
| codex-agenttalk-developer-5 | Main team | dev-5 |
| claude-agenttalk-lead | Main team | lead |
| qwen-agenttalk-reviewer-4 | Main team | rev-4 |
| claude-shopfront-developer-1 | Shopfront team | dev-1 |
| claude-shopfront-developer-1 | Cross-team list | shopfront/dev-1 |
| codex-agenttalk-developer-1 | Two dev-1s on a team | x.dev-1 |
| nightly-docs-builder-agent | Doesn't fit | nightly-docs-buil… |

Reference implementation + tests: `reference/name-shortener.js`.

## Avatars
- Silhouette follows the **theme**: Midnight hexagon · Paper rounded-square · Synthwave star · Terminal none (runtime letter in a 1px box, runtime colour). Other directions: 1b oval-vivid, 1c triangle, calm baseline oval-muted.
- Runtime badge bottom-right: Claude **C**, Codex **X**, Qwen **Q** — colours in `tokens/themes.json → runtime` (light set for Paper).
- Health never lives in the avatar; it's the status line.
- Never crop shaped avatars into circles — the shape is the alpha channel. Render with `object-fit: contain` / `background-size: contain`.
- Files: `prototype/new_avatars/<shape>/<motif>.png`. Assignment by index in the prototype; production should store an explicit agent → file mapping.

## Contrast — Loud (1c) themes, measured (WCAG 2.x)
| Theme | Body (ink on bg) | Card (ink on paper) | Meta (mute on bg) | Highlight chip (hlInk on hl) | Hot block (hotInk on hot) | Long text OK? |
|---|---|---|---|---|---|---|
| acid | 17.51:1 AA | 16.26:1 AA | 5.86:1 AA | 16.28:1 AA | 5.44:1 AA | Yes |
| bubblegum | 15.94:1 AA | 19.03:1 AA | 5.02:1 AA | 3.04:1 large only | 5.88:1 AA | Yes |
| riso | 8.56:1 AA | 9.49:1 AA | 3.67:1 large only | 2.88:1 FAIL | 2.88:1 FAIL | No |
"large only" = OK for ≥ 24px regular or ≥ 18.66px bold. Riso: darken `mute` and put dark ink on its red blocks before using it for long text.

## Contrast — app themes (fg/dim/accent), measured
| Theme | fg on bg | fg on panel | dim on bg | dim on panel | accentInk on accent |
|---|---|---|---|---|---|
| midnight | 16.23 | 15.29 | 5.27 | 4.97 | 6.57 |
| paper | 15.52 | 17.19 | 3.59 | 3.97 | 3.68 |
| synthwave | 17.44 | 16.25 | 6.72 | 6.26 | 6.79 |
| terminal | 15.89 | 15.30 | 6.30 | 6.07 | 18.72 |

**Action needed — Paper:** `dim` on bg is 3.59:1 and white on the orange accent is 3.97:1 (both below 4.5). Suggested: dim `#6B655B` (5.21:1 on bg) and accent `#C9481F` (4.74:1 with white). Verify against the final palette.

## Data we don't have yet
Live today: roster & health · messages & threads · needs-a-human queue · gates & evidence · risk register · ownership map · lessons & onboarding · lead chat · Claude & Codex usage windows.

| Not yet | What the console does |
|---|---|
| Mission progress & ETA | Removed from every header. The lead says where things stand, in words. |
| Program stages | Stays a concept (2c). Not in the app. |
| Decision deadlines & options | Optional. No deadline → show waiting age; options are the lead's quick replies. |
| Signed export | Hidden until the server can sign. |
| Qwen spend in the console | Designed (3b). Behind a flag until the gateway ledger is readable. |
| Both machines in one view | Switcher designed; each team is its own source. Until supported, each team opens its own console. |
