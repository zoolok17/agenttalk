# Provenance and third-party terms

## Where this material comes from
- **Designer's work.** The specs (`docs/`), tokens, fixtures, reference code, prototype pages and the shaped avatars were made for agenttalk by the operator's designer, working with the operator, in September 2026. The operator gave permission to commit them to this repository on 2026-09-26.
- **Licence of the designer's work: TO BE CONFIRMED by the operator.** Until confirmed, do not assume the repository's MIT licence covers these files, and do not reuse them outside agenttalk.
- **Avatars.** The 30 shaped avatars in `prototype/new_avatars/` come with the handoff. Their authorship and generation tool are not recorded here yet. The same gap applies to the ten round avatars already in `src/agenttalk/web_static/avatars/`.
- **Not included: the design tool's runtime** (`prototype/support.js`, a generated bundle). Its redistribution terms are not established, so it stays out of this repository. Without it the prototype pages do not render; the full handoff zip with the operator does. The specs in `docs/` are complete without the prototype.

## Third-party material that the pages LOAD (not bundled here)
Opening a prototype page or a design-system guideline page fetches these from the internet. Nothing below is stored in this repository.

| What | From | Licence |
|---|---|---|
| Space Grotesk, Geist, Geist Mono, JetBrains Mono, Sora, Archivo, Space Mono, Instrument Serif | fonts.googleapis.com / fonts.gstatic.com | SIL Open Font License 1.1 |
| React 18, React DOM 18 | unpkg.com | MIT |
| Babel standalone 7 | unpkg.com | MIT |

Versions differ between files (for example `react@18.3.1` in the runtime but a floating `react@18` in `navigation.card.html`), and the pages use no Subresource Integrity. Loading the fonts also tells Google the viewer's IP address.

## Rules for this folder
- **View offline only, never publish.** Do not serve these pages from GitHub Pages or any public host: they run unauthenticated third-party scripts. agenttalk itself never serves them.
- **The production console must not copy the prototype's approach.** It self-hosts any fonts it uses, together with their OFL licence texts. It keeps the console's Content-Security-Policy (no inline styles, no external scripts). It renders bus data with `textContent` only, and allow-lists URL schemes for any link built from data.
