# 02 · Decisions (dev team questions and notes, answered)

| # | Question / note | Answer | Where |
|---|---|---|---|
| Q1 | Is 1d the whole app? Terminal as a theme? Shortcuts everywhere? | **Yes.** The Conversation is the app. Terminal is its 4th theme (all fonts → JetBrains Mono, all radii → 0; nothing else changes). Same keys in every theme. | 3a |
| Q2 | One budget with alert, cutoff and a late bill? | One bar against the cap. Ledger = fill (live). Alert = dashed line. Cutoff = solid line. Cap = bar end. Bill = a tick with its own timestamp. Projection = dotted extension. | 3b |
| Q3 | A quiet day? | A sentence, not a dashboard: **"All quiet."**, then "N of M agents are idle — that's their resting state", then *Since you last looked* (3 lines). No empty panels, no amber. | 3a (Quiet), 3c |
| Q4 | "I can't see the team right now"? | Banner first ("win-ws01 stopped reporting 3h 12m ago… frozen at 18:22"), everything greyed to 50% and stamped *as of 18:22*, pulses stop, composer and action buttons disabled with the reason, "Retry now". | 3a (Can't see team), 3c |
| Q5 | Decisions with no deadline? | **Yes — deadline is optional.** Without one, show age: "no deadline · waiting 2d". Queue order: stuck agents first, then oldest. | 3a |
| Q6 | Silhouette per theme? Show the runtime? | Midnight → hexagon, Paper → rounded square, Synthwave → star, Terminal → no image (runtime letter in a box). Runtime = corner badge **C / X / Q**. Health is never in the avatar. | 3d |
| Q7 | Which Loud themes pass for long text? | Measured (see 06-RULES): **acid and bubblegum pass** body, card and meta text. **riso fails** meta text (3.67:1) and its highlight/hot blocks (2.88:1). bubblegum's highlight chip is 3.04:1 → big bold words only. | 3d |
| N2 | Qwen capacity as money | Qwen rows show **€ today**; the rail has a budget block. Claude/Codex keep % windows with resets. | 3a, 3b |
| N3 | Phone layout | Needs you → budget → lead's latest message; 48 px buttons; tab bar (Needs you / Team / Lead); one team per screen. | 3c |
| N4 | Two machines / teams | Header switcher, one chip per team: name, machine, own freshness dot, needs-you count. Keys 1 / 2. Each team is its own data source; there is no merged view yet. | 3a |
| N5 | Name shortening rule | `codex-agenttalk-developer-5` → `dev-5`. Runtime → badge; project dropped when it's the current team; role abbreviated; ties get `c.`/`x.`/`q.`. | 3d |
| N6 | "spec-kitty" in baseline header | Removed, plus the "WP 7/12" progress. Header now: "Main team · win-ws01 · 10 agents". | Team Console |
| — | Busy can look stuck | dev-5 (tests running 6m, output 40s ago) gets **no card** — it's listed under "Also happening · not for you". dev-6 (no output 14m, tests exited 12m ago, no reply) gets a card with Restart. | 3a |
| — | Only data that exists | Mission progress/ETA, program stages, deadlines, signed export: removed from the app. Qwen spend and cross-machine: designed, behind flags. | 3d |

## Round 2 (team lead's reply to handoff v2)
| # | Note | Change |
|---|---|---|
| R1 | Cap and bill are account-wide; each machine's gateway has its own ledger, alert and cutoff (main 39/44 €, second 35/39 €). | **Two levels.** Team bar = its ledger vs its own alert + cutoff, labelled "of 44 € cutoff", never "of 100 €". Account bar = both ledgers stacked vs the 100 € cap, bill tick on top. Spec: 04. |
| R2 | `claude-agenttalk-frontend-dev` broke the rule. | Everything between the project and the optional number is the role, hyphens included; each word abbreviated → **fe-dev**. Projects matched against the known team list. Code: `reference/name-shortener.js`. |
| R3 | Process status (running/exited) isn't visible yet. | Fallback evidence from heartbeat + progress counter + last message: "No progress for 14 min · no reply sent · heartbeat still fresh". With only this, **Wait is the primary option** and Restart second, with the line "Weaker evidence: process status isn't visible yet, so Wait comes first." Prototype tweak `processEvidence` shows the stronger version. |
| R4 | Changing gateway limits is an operator machine action. | "Raise to 54 €" only sends the answer to the lead. Confirmation reads "✓ Sent to the lead: Raise to 54 € · the limit itself changes on win-ws01, from the command line". The console never changes limits. |

## Correction to the chat summary
The chat said "only acid passes for long text". The measured table (06-RULES) shows **bubblegum passes too**; the board 3d computes this live and is correct.
