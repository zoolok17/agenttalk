---
affected_files: []
cycle_number: 1
mission_slug: team-scope-0150-01KTC934
reproduction_command:
reviewed_at: '2026-06-05T17:53:20Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP05
---

# WP05 review: rejected

## Finding

### Blocking: README install instructions still pin `v0.14.0`

`pyproject.toml` and `src/agenttalk/__init__.py` are bumped to `0.15.0`, and `CHANGELOG.md` adds the `0.15.0` release entry, but the README's canonical install snippets still tell users to install the old `v0.14.0` tag:

- `README.md:21` - TL;DR one-time install pins `git+https://github.com/zoolok17/agenttalk.git@v0.14.0`
- `README.md:126` - Install section pins the same old tag
- `README.md:129` - replacement guidance still names `v0.14.0`

This is release-prep drift for WP05/T019-T020. A user following the updated 0.15.0 README would install 0.14.0 and not get the newly documented team-scope CLI behavior/skills. Update those release-facing install references to `v0.15.0`, or make the snippet intentionally generic without pointing at the previous tag.

## Verification performed

- Resolved Spec Kitty review context for mission `team-scope-0150-01KTC934`, WP05.
- Confirmed changed files stay within WP05 `owned_files`.
- Ran `git diff --check e69e37b..1c24638`: passed.
- Inspected README, SECURITY, CHANGELOG, version files, and changed Claude/Codex skill files.
- Ran full suite with Python 3.14 and `PYTHONPATH=src`: `548 passed, 2 skipped`.
- Ran `ruff check .`: passed.

## Residual risk

I did not run the quickstart/install-skills release spot check after finding this blocker. Re-run that after the README tag pin is corrected.
