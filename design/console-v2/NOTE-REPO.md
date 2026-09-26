# About this copy of the handoff

This folder is the designer's console handoff v2 (decided direction: *The Conversation*), committed so the people building the console have the specs, tokens, fixtures, reference code and prototype in the repository. Start with `README.md`.

It is design material, not product code:
- It is not part of the Python package. The sdist uses an explicit include list, and `design/` is not on it.
- The prototype runtime (`prototype/support.js`) loads React and Babel from a public CDN when you open a prototype page. That happens only in your browser, only when you view the design; agenttalk never serves these files.

## Left out of the repository copy (to keep it small)
- `prototype/new_avatars/oval-muted`, `oval-vivid` and `triangle`. These silhouettes belong to directions that were not chosen. The chosen themes use `hexagon` (Midnight), `rounded-square` (Paper) and `star` (Synthwave), and those are included.
- `assets/avatars-shaped/contact-sheet.png` (a preview of all 60 avatars).
- `prototype/avatars/`. These ten round avatars are byte-identical to `src/agenttalk/web_static/avatars/`. To view `Team Console.dc.html` with images, copy that folder next to it as `prototype/avatars/`.
- `design-system/components/core/core.card.html` and `design-system/components/patterns/patterns.card.html`. These are gallery preview pages. The components themselves (`.jsx`, `.d.ts`, prompt notes) are included.

The full original handoff (a zip) is with the operator.

## One change to the designer's files
`prototype/Console Futures.dc.html` lines 37 and 368 carry a `nosemgrep` comment. The repository's security scan reads the prototype's `{{ ... }}` template placeholders as server-side template injection. They are design-time placeholders in a file agenttalk never serves.

## Open notes from the lead
These were sent back to the designer:
1. The monthly cap and the provider bill are account-wide, while each machine's gateway has its own ledger, alert level and cutoff. The budget view needs an account level and a per-team level.
2. `claude-agenttalk-frontend-dev` does not fit the name-shortener pattern, because its role contains a hyphen.
3. Process-level evidence for stuck cards ("test run exited") is not in the snapshot yet, so the cards need fallback wording.
4. Raising a cap is an operator machine action. In the console it is a message to the lead, not a button.
