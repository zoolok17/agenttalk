# Implementation Plan: 0.15.0 Team Scope

**Branch**: `master` | **Date**: 2026-06-05 | **Spec**: [spec.md](spec.md)

No open planning questions: designs were settled in the published issue
bodies (#15/#16/#17, themselves cross-reviewed) and the 0.14.0 codebase
is freshly known (same maintainer pair, same constraints).

## Summary

Close the team-scale friction cluster: role-scoped broadcast audiences
with honest not-applicable replies (#15), broadcast delivery accounting
(#16), and recoverable quarantine of invalid messages (#17). All
additive on the 0.14.0 store/derivation model.

## Technical Context

**Language/Version**: Python 3.10+ (CI matrix 3.10-3.13 x 3 OSes), stdlib only
**Storage**: existing file-backed store; NEW `.agenttalk/quarantine/` dir (outside messages_dir, invisible to scanning by construction)
**Testing**: pytest, in-process cli.main pattern; dev-install + PYTHONPATH-in-worktree gotchas apply; **CI gate before tag (NFR-005)**
**Target Platform**: Windows-first, POSIX-portable
**Performance**: prune 10k messages <= 10 s (single scan + N moves; no quadratic)
**Constraints**: C-001..C-009 (spec); exit 5 = partial fan-out (new, documented; 0/1/2/3/4/130 untouched)
**Scale**: rosters <= ~8, stores <= ~10k messages

## Charter Check

Skipped - no charter exists. C-tables + SECURITY.md trust model are the gates.

## Structure (all additive, module-layered like 0.14.0)

```
src/agenttalk/
  store.py    resolve_role_audience, quarantine machinery (list+move),
              batch helpers; NO config-derivation coupling (C-004)
  threads.py  responded_na tracking (broadcast) + na_response label (pairwise)
  cli.py      broadcast --to-role + freeze meta + batch_total + exit-5
              partial-failure manifest; reply --na (+ FR-006 refusal);
              prune command; status/doctor wiring + incomplete-batch warning
  doctor.py   quarantine/invalid counts check
tests/        test_store, test_threads, test_teams, test_cli,
              test_doctor, test_coordination (e2e gates)
```

## WP ordering (dependency truth for /spec-kitty.tasks)

1. WP01 engine (store+threads+unit tests) — no deps
2. WP02 CLI surface (cli.py+test_cli) — deps WP01
3. WP03 doctor (doctor.py+test_doctor) — deps WP02
4. WP04 e2e gates (test_coordination) — deps WP02, WP03
5. WP05 skills/docs/release prep — deps WP04

Single lane, serial; per-WP Codex review (C-008); fresh-eyes + CI gate
before tag.
