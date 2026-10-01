---
reviewed-against: "0.95"
---

# Plain-language voice

In plain words: this file describes how agents write text that a person will read. It
holds the rules every kind of such text shares, then one short section per kind (release
notes and similar, documentation, code comments, interface text), each with its own length
and shape. The writing skills point here instead of each keeping their own copy. Messages
between agents are not covered; see "Out of scope" at the end.

## The reader
- Name the reader before you write. For most kinds it is a person who uses the software,
  pays for it or decides about it, and who may never have written code. For code comments it
  is a developer who knows the language but not this code's history.
- The reader has time for clear text. What stops them is jargon, missing context and lists
  of file names where an explanation should be.

## Core rules (every kind)
- Use simple words and short sentences, one idea per sentence. Say who does what.
- **The plain words must be true for this case, not in general.**
- Before you replace a term, ask what your plain version now claims that the original did
  not: where, which branch or version, who, how many, how long, in what order. Add those
  details from the source, or keep the term and explain it in a sentence.
- Explain every technical term the first time it appears, or avoid it.
- Never invent a number, a name or a place that the source does not give.
- Keep every fact. The one exception is anything sensitive: secrets, passwords, keys and
  tokens; private or customer data; internal-only paths, hostnames or addresses. Remove it,
  or replace it with a safe summary that keeps the meaning ("a key was rotated"). Moving it
  to a technical section does not make it safe.
- Put technical detail last. Where a kind has no closing technical section (code comments,
  interface text), keep only the detail the reader needs and leave the rest to the commit
  message or the pull request.
- Never let one plain phrase stand for two different facts. Two pairs that are often mixed
  up:
  - **Released and deployed.** "Released" means a new version can be installed; it does not
    mean anyone is using it yet. "Deployed" means a running system was switched over to it,
    so people are using it now.
  - **Null, absent and empty.** The field is there but set to "no value" on purpose (null),
    the field is left out entirely (absent or undefined), or the field is there but empty (an
    empty text or an empty list).
- Be honest about limits and about what was and was not checked.

## By kind of text

### A. Release notes, changelog entries, pull request descriptions, issue and review comments, status reports
- Applied by: write-for-humans, which owns these and carries their templates.
- Structure: what changed, why it matters, what you will notice, what to do, then Technical
  details. "Before this ... Now ..." only where something changed. A review comment keeps the
  review's own format and severity tag; only the words get plainer.
- Length: as long as it takes to answer those questions clearly. Do not compress to look
  efficient.

### B. Documentation: READMEs, guides, tutorials, reference pages, manuals, specifications and design documents
- Applied by: write-docs when writing, review-docs when checking.
- Structure: every document opens with an **"In plain words"** summary: what it is, who it is
  for and what you can do with it. For a specification or a design document, also say what it
  decides and what it leaves open. Then the body, in the shape its type needs (a tutorial's
  steps, a reference's tables).
- Length: the summary is about 3 to 6 sentences. The body is as long as the reader's task
  needs.
- Precision first in reference material: exact names, flags, commands, fields and values stay
  in place, in code font, never replaced by a paraphrase. Explain each one in plain words the
  first time it appears.
- Scope: apply this to documents you write or change. Do not rewrite untouched documents only
  to change their voice.

### C. Code comments
- Applied by: craft-code.
- Structure: say why, not just what: the reason, the constraint, the trap. No "before" and
  "now". The full story (what went wrong, which review found it) belongs in the commit message
  or the pull request, not in the code.
- Length: one or two sentences. Never an essay.
- Words: plain words. Spell out an abbreviation or a term specific to this project the first
  time it appears in a file, or point to where it is defined. Standard terms of the language
  or of a widely known library need no explanation.

### D. Interface text: labels, buttons, menus, empty states, error messages, tooltips
- Applied by: whoever writes it; craft-code when a change adds or edits interface text.
- Structure: say what the person can do next. An error message says what happened and what to
  try, and never blames the reader: "This needs a number from 1 to 60", not "You entered an
  invalid value".
- Length: as short as possible. A label is a word or two; a message is one sentence where it
  can be.
- Words: use the same name for the same thing everywhere in the interface and the
  documentation. Never show a secret, a token or an internal path, including in error details.

## Out of scope: text for agents
Messages between agents, task briefs, bus traffic and instructions written for agents (skills,
prompts) stay compact and precise. Agents read them, and every extra word costs input. When a
person also reads a message, for example a status report shown to the operator, write that
message as kind A.
