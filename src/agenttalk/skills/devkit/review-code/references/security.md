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
