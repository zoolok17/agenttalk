---
name: _shared
description: >-
  Shared reference material for the devkit skills (the canonical evidence profiles and the
  routing index). This is NOT an invocable capability skill - do not run it; the other
  devkit skills link to its references/ files.
category: reference
reviewed-against: "0.42"
---

# _shared (reference holder - do NOT invoke)

This directory is not a capability skill. It exists only to bundle shared reference material
that the other devkit skills link to, installed alongside them under the same Agent-Skills
directory (so a skill's `../_shared/references/...` link resolves at the install location):

- references/evidence.md - the canonical typed-evidence output profiles every skill emits.
- references/routing.md - the task-to-skill routing index, negative triggers, capacity
  guidance, the dual-review-mode rules, and the lead GO checklist.

Do NOT invoke `_shared`. If you are choosing which capability to apply, read
references/routing.md. If you are about to emit evidence, read references/evidence.md (your
skill also carries a short in-skill stub of its profile so you have the field shape even
without opening this file).
