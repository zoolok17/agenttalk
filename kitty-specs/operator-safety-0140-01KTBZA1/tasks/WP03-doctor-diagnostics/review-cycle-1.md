---
affected_files: []
cycle_number: 1
mission_slug: operator-safety-0140-01KTBZA1
reproduction_command:
reviewed_at: '2026-06-05T15:08:04Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP03
---

# WP03 review feedback

Status: rejected

## Blocking findings

1. Doctor still violates the root-first output contract.

   WP03 T016 and the Definition of Done require `agenttalk doctor` to start with `root: <path>`, and T016 also requires the root to be the first key in structured output. The implementation leaves the human renderer unchanged at `src/agenttalk/cli.py:1804`, so live output starts with:

   ```text
   agenttalk doctor - D:\Projects\claude\agenttalk\.worktrees\operator-safety-0140-01KTBZA1-lane-a
   ```

   The JSON path is also unchanged: `src/agenttalk/doctor.py:49` returns `agenttalk_version` first and `project_root` third, so `agenttalk doctor --json` starts with:

   ```json
   {
     "agenttalk_version": "0.13.0",
     "python_version": "3.14.3",
     "project_root": "..."
   }
   ```

   This is not just presentation polish: `contracts/cli-surface.md:76`, `spec.md:81`, `spec.md:152`, `WP03 T016`, and the WP03 DoD all make this normative. Because `cli.py` is outside WP03's current `owned_files`, please either add the necessary one-line/addendum scope for the CLI renderer or explicitly route the ownership change through the mission before re-review. Add a regression assertion for the CLI first line and the structured-output first key; current `tests/test_doctor.py` only round-trips JSON and does not enforce the WP's first-line/root-first-key requirement.

## Verified

- Changed files are limited to WP03 owned files: `src/agenttalk/doctor.py`, `tests/test_doctor.py`.
- `git diff --check e5600404da7a761367185dcbed5613d7400e4308..a48de2551e10cb4a2065104432adc35233409f23` passed.
- `python -m pytest tests\test_doctor.py -q`: 13 passed.
- `python -m pytest -q`: 491 passed, 2 skipped.
- Multi-store and liaison logic otherwise matched the WP intent during inspection.
