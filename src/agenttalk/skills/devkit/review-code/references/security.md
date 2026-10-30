# Security pass (OWASP-aligned) — review-code reference

Run this focused pass when a change touches **auth, input handling, data access,
secrets/crypto, file/path handling, deserialization, or dependencies**. Ground every
item in the actual code — do not assert a risk (or its absence) from plausibility.
Treat confirmed security findings as `[blocker]`/`[major]` by default.

## OWASP-aligned security sweep
- **Broken access control** — every sensitive operation checks **authorization** for
  the acting principal, not just authentication. No IDOR / missing object-level checks
  (a user can't act on another user's object by changing an id).
- **Input validation** — parameterized queries (no string-built SQL); validate /
  allowlist input at trust boundaries; no command / template / path injection.
- **Output encoding** — context-correct escaping to prevent XSS; no raw interpolation
  into HTML / JS / SQL / shell. Untrusted data is rendered escaped, everywhere it appears
  (body, subject, attributes, metadata, names).
- **Secrets & crypto** — no hardcoded keys / tokens / passwords; secrets come from env /
  secret store. Passwords hashed with bcrypt/scrypt/Argon2 (never MD5/SHA-1).
  Cryptographically strong randomness for tokens/keys; constant-time compare for secrets.
- **Error & log hygiene** — no stack traces, secrets, tokens, or PII leaked in error
  messages or logs.
- **Dependency / supply chain** — flag new or updated dependencies. **Confirm every
  suggested package actually exists** and is the genuine, maintained one — a meaningful
  fraction of agent-suggested packages don't exist (a slopsquatting vector). Pin versions.
- **Safe file/path handling** — canonicalize and bound paths (no `..` traversal);
  validate upload type/size; no unsafe deserialization of untrusted data.
- **Resource safety** — close handles/connections on success AND error paths; bound
  caches/queues/buffers; time out external calls.

## Posture
- AI-generated code carries security bugs at a materially higher rate than
  human-written code — run this pass even when the functional behavior demonstrably works.
- Keep language/framework-specific checks (ORM specifics, framework CSRF defaults,
  templating auto-escape behavior) appended below as the codebase warrants; load them
  only when the change is in that stack.

## The builder's security lines
When the handoff has a `security-note:` and `security S<n>:` lines, check them too ([the
rules](../../_shared/references/secure-coding.md), step 3, "What the reader checks"). A
filled-in list proves nothing; the code and the checks that ran are the evidence.
- **The note** is recorded before the first code commit (or says it is late), names the
  right boundary, and misses no other boundary of the change.
- **Each `executed` line:** the command exists and ran on the exact commit handed over.
  Remove that line's protection (a copy of the code with that one change) and see the
  named command fail. A command that stays green without the protection, or a line whose
  words claim more than the command covers, is a finding.
- **Each `checked by reading` and `does not apply` line:** the reason is true for this
  change, and `checked by reading` is used only for a promise the rules mark that way.
- **One form per line:** no line mixes `executed` and `open`; every unfinished part has
  its own `open` line with an owner.
- **Findings name the principle** (`S1` to `S10`) they break, next to the usual severity tag.
