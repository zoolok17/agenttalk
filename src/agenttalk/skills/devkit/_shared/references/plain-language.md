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
- Keep every fact. The one exception is anything sensitive:
  - secrets (passwords, keys, tokens) are removed from every text, whoever reads it;
  - private data is shown only to a reader who is authorized to see it for this purpose:
    their own data, or data the product's access rules let them see. A screen that shows
    `Your delivery address is {customer.address}` to that customer is correct, and so is a
    fulfilment screen that shows the address to the staff member handling the order;
  - private data is never shown to anyone who is not authorized to see it, and real private
    or customer records are never pasted into examples, prose, logs or public pages: remove
    them, or summarise them safely;
  - this is a writing rule: it never replaces or invents the product's own access checks;
  - internal-only paths, hostnames and addresses are removed from anything a public or
    unauthorized audience sees (release notes, a public page, an error sent to a remote
    caller). Local diagnostics and private run guides keep the path, or another safe
    identifier, when it is what lets the person fix the problem.

  Remove it, or replace it with a safe summary that keeps the meaning ("a key was rotated").
  Moving it to a technical section does not make it safe. This rule never forces a choice
  between saying what to do next and keeping something private: say what to do with what
  the reader is allowed to see.
- Put technical detail last. Where a kind has no closing technical section (code comments,
  interface text), keep only the detail the reader needs and leave the rest to the commit
  message or the pull request.
- Never let one plain phrase stand for two different facts. Two pairs that are often mixed
  up:
  - **Released and deployed.** "Released" means a new version can be installed; it does not
    mean anyone is using it yet. "Deployed" means a running system was switched over to it,
    so people are using it now.
  - **Absent, null, undefined and empty.** Wherever the language or the data format tells them
    apart, they are different facts, so say which one it is: the field is left out entirely
    (absent); the field is there but set to "no value" (null); the field is there but holds
    "undefined" (in JavaScript, `{field: undefined}` still has the field, while `{}` does not);
    or the field is there but empty (an empty text or an empty list).
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
- Proposals: a specification or a design document may describe what is not built yet, when its
  status is labelled (a `Status: proposed` line, and "will" / "proposed" wording). Never
  present an unimplemented proposal as something the software already does. An example of
  proposed behavior is marked "proposed, not runnable yet" and checked against the proposal,
  not run. Documentation of the current product describes only what has shipped.
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
  documentation.
- Privacy: never show a secret (a password, a key, a token), anywhere, including in error
  details. Show private data only to a reader the product authorizes to see it (their own
  data, or data their role lets them see), never to anyone else; the text never replaces the
  product's access checks. Hide internal paths and hostnames only from people who should not see them: a
  public page, or an error sent to a remote caller. A local diagnostic keeps the path, for
  example `cannot save the order: /var/lib/shop/orders.db is read-only`, because it is what
  lets the person fix the problem.

## Out of scope: text for agents
Messages between agents, task briefs, bus traffic and instructions written for agents (skills,
prompts) stay compact and precise. Agents read them, and every extra word costs input. When a
person also reads a message, for example a status report shown to the operator, write that
message as kind A.
