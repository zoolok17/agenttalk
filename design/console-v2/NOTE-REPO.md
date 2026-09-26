# About this copy of the handoff

This folder is the designer's console handoff v2 (decided direction: *The Conversation*), committed so the people building the console have the specs, tokens, fixtures, reference code and prototype in the repository. Start with `README.md`.

It is design material, not product code:
- It is not part of the Python package. The sdist uses an explicit include list, and `design/` is not on it.
- The design tool's runtime (`prototype/support.js`) is NOT included, because its redistribution terms are not established (see `LICENSE-NOTES.md`). Without it the prototype pages do not render; open the full handoff zip instead. The specs in `docs/` are complete on their own.

## Left out of the repository copy
- `prototype/new_avatars/oval-muted`, `oval-vivid` and `triangle`. These silhouettes belong to directions that were not chosen. The chosen themes use `hexagon` (Midnight), `rounded-square` (Paper) and `star` (Synthwave), and those are included.
- `assets/avatars-shaped/contact-sheet.png` (a preview of all 60 avatars).
- `prototype/avatars/`. These ten round avatars are byte-identical to `src/agenttalk/web_static/avatars/`. To view `Team Console.dc.html` with images, copy that folder next to it as `prototype/avatars/`.
- `design-system/components/core/core.card.html` and `design-system/components/patterns/patterns.card.html`. These are gallery preview pages. The components themselves (`.jsx`, `.d.ts`, prompt notes) are included.

The full original handoff (a zip) is with the operator.

## One change to the designer's files
The two lines in `prototype/Console Futures.dc.html` that contain `href="{{` carry a `nosemgrep` comment. The repository's security scan reads the prototype's `{{ ... }}` template placeholders as server-side template injection. They are design-time placeholders in a file agenttalk never serves.

## Notes from the lead
The lead's four notes on the first v2 (the budget levels, a name that broke the shortener, stuck-card evidence without process data, and raising a limit) were answered by the designer in round 2. See `docs/02-DECISIONS.md` ("Round 2"). The updated name rule shortens all 15 real agent names correctly.
