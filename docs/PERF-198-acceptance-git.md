# Acceptance Git process measurements

Audience: maintainers evaluating the Windows acceptance-test cost in issue #198.

The supplied 22-test sample used **927 Git processes instead of 1,576**, a
**41.2% reduction**. Median elapsed time fell from **96.15 s to 75.70 s** (21.3%).
The requested 50% process-reduction target was **not reached**. These are local
sample measurements, not a prediction of the complete Windows CI job time.

## Scope and verification semantics

Each `close` CLI invocation owns a fresh context-local cache. A direct
`acceptance.resolve()` call also owns a scope unless it is already inside one.
Only Git metadata and history-query results are reused. Cache keys distinguish
the repository, revision/query and environment. Nested CLI calls get independent
scopes; exceptions clear the scope; returned metadata is copied before exposure
to callers. Bare `verify_project()` library calls remain fresh.

For full SHA revisions, project metadata is read once per command. Symbolic refs
are resolved on every call. **Live HEAD equality and dirty-state checks still
run at every existing live verification point**, including both preparation and
freeze. Before this change, another process could move HEAD or edit tracked or
untracked files between those two reads. Reusing an entire successful live check
would miss that transition; this implementation deliberately does not do so.
The regression tests introduce both a dirty file and a committed HEAD move
between preparation and freeze and require refusal.

Root-set/tree facts are tied to verified object IDs; history and patch IDs are
reused only within the current operation. As with the cooperative acceptance
profile, manually replacing/pruning objects, changing Git history configuration,
or rewriting a repository during an operation is not an atomic filesystem
transaction. Those mutations must be quiescent. No cache survives into the next
command. This is a consistent operation-local view of object/history facts, not
a claim that the filesystem cannot change while Git runs.

The batched queries preserve the original checks:

- `rev-parse --show-toplevel --verify --end-of-options <revision>^{commit}`
  emits the root followed by exactly one verified commit. The root is still
  compared to the requested checkout, nonzero exit is refused, and the commit
  must still be a full SHA-1. Caller input stays after `--end-of-options`.
- On an initial live read, `rev-parse HEAD <verified-sha>^{tree}` returns HEAD
  and tree together. Both inputs are controlled at that point; a nonzero exit
  still fails, and HEAD must equal the verified commit. The separate status
  query retains `--porcelain --untracked-files=all` and the same dirty diagnostic.
- Source-history queries retain `GIT_NO_REPLACE_OBJECTS=1`, complete-history
  checks, full-SHA validation and their existing return-code interpretation.
  The cache does not retain large diff/patch inputs; only the validated change
  identity is reused.

Retained files, source enumeration, gate/knowledge state, approvals, obligation
discovery and verdicts are **not cached**. Publication locking and the final
audit still execute as before. No acceptance policy, comparator, schema or
required assertion was changed.

The fixtures build one synthetic history per session and retain base and
candidate snapshots of it. Every test copies its own complete repository:
objects, refs, index and configuration. There are no hardlinks or alternates.
The project ID is derived once from the verified session root; production open
and publication paths still verify each copied repository. A regression corrupts
a copy's object and changes its config/index/HEAD while checking that the
template remains intact. All existing acceptance test functions and their
parameterizations are AST-identical to the baseline; only fixture setup changed.

## Measurement protocol

Baseline: `7bd002344d1713084ee527875ea67b688c0c3129` on master, including PR #197.
Environment: Windows build 22631, CPython 3.10.11 AMD64, pytest 9.1.0,
Git 2.39.1.windows.1. This was a shared development host without CPU pinning;
background host activity was not controlled.

Each side ran the same supplied sample three times in fresh Python processes,
with a fresh scratch basetemp each time. Template creation is included in the
after count and time. There was no excluded warmup: the workload is a cold pytest
invocation. No outlier was removed. The summary statistic is the median; the
range is reported to expose timing variation. Git process count was the primary
metric, with an a priori target of at least 50% fewer processes.

The supplied `gitcount_plugin` instruments `subprocess.Popen.__init__`, counting
Git invocations from the pytest process (not Git's own internal child processes).
It groups `git -C <repo> <subcommand>` calls by subcommand; `git -c ...` remains
one reported category. Each of the three runs on a side had identical counts.

| Run | Before processes | After processes | Before seconds | After seconds |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1,576 | 927 | 97.82 | 75.63 |
| 2 | 1,576 | 927 | 96.15 | 75.70 |
| 3 | 1,576 | 927 | 95.91 | 77.16 |
| Median | 1,576 | 927 | 96.15 | 75.70 |

Mean processes per selected test: 71.64 -> 42.14. Maximum attributed to one
selected test: 155 -> 97. Session setup is attributed to the first test that
requests it, so individual-test counts depend on sample order.

| Git category | Before | After |
| --- | ---: | ---: |
| rev-parse | 870 | 432 |
| rev-list | 162 | 105 |
| status | 125 | 108 |
| -c | 116 | 104 |
| merge-base | 91 | 85 |
| commit | 64 | 29 |
| add | 46 | 25 |
| config | 44 | 2 |
| checkout | 26 | 26 |
| init | 22 | 1 |
| branch | 6 | 6 |
| rebase | 2 | 2 |
| merge | 2 | 2 |

The 17 fewer status calls come from verifying the shared fixture identity once,
not from removing live product checks. A production-only intermediate sample
passed all 22 cases with 1,171 processes in 83.64 s. Adding the base template and
direct-resolver scope gave 1,023 processes in 78.76 s; sharing the fixture identity
and candidate snapshot produced the final counts above. These intermediate
timings are single diagnostic runs, not repeated benchmark claims.

With `W` the checkout, `C` the supplied counter-plugin directory, `S` isolated
scratch and `sampleFile` the supplied node-list file, the PowerShell command was:

```powershell
$env:PYTHONPATH = "$W/src;$C"
$env:PYTHONDONTWRITEBYTECODE = '1'
$sample = Get-Content $sampleFile
py -3.10 -m pytest -q -p no:cacheprovider -p gitcount_plugin -s @sample --basetemp "$S/run-N"
```

Each sample-file line starts with `tests/test_acceptance.py::`, followed by one
of these exact node IDs, in this order:

```text
test_acceptance_acks_without_bundle_hold
test_acceptance_malformed_bundle_cannot_attach[digest]
test_acceptance_invalid_plan_refused_before_close_creation[duplicate]
test_acceptance_missing_gating_bytes_is_unmeasured_not_failed
test_acceptance_reopen_creates_linked_successor
test_acceptance_exact_operator_policy_amendment_preserves_failure[operator-child-failure]
test_acceptance_target_coverage_reshape_table[split-equivalent-False]
test_acceptance_final_cold_rejects_author_or_claims_exposure[authored_in_scope]
test_acceptance_cold_blocking_residual_is_additive[open]
test_acceptance_withheld_plan_digest_cannot_be_mislabelled[source]
test_acceptance_gate_retains_earlier_evidence_actor
test_acceptance_informational_required_artifact_integrity_holds[corrupt]
test_acceptance_recovery_obligations_follow_change_table[cold-earlier-same-sha]
test_acceptance_recovery_obligations_follow_change_table[corrupt-earlier-rebase]
test_acceptance_recovery_obligations_follow_change_table[hygiene-earlier-squash]
test_acceptance_recovery_obligations_follow_change_table[final-review-earlier-different]
test_acceptance_recovery_obligations_follow_change_table[approval-later-same-sha]
test_acceptance_recovery_obligations_follow_change_table[routing-later-rebase]
test_acceptance_recovery_obligations_follow_change_table[isolation-later-squash]
test_acceptance_recovery_obligations_follow_change_table[unfinished-source-later-different]
test_acceptance_recovery_dispositions_are_bound_to_sources_and_seal[after-seal]
test_acceptance_schema3_cannot_attach_without_required_hygiene[environment]
```

## Regression evidence

The initial new regression run was **1 failed, 7 passed**: the process-budget
assertion observed two project metadata reads instead of one. The first product
change made that run **8 passed**. The cache-lifecycle and independent-copy tests
then passed with the focused file (**10 passed**).

The final foreground targeted run passed **1,173 tests, with 1 skipped**, in
**2,233.05 seconds** (37m13s), using Python 3.10 and `PYTHONPATH=<checkout>/src`:

```text
py -3.10 -m pytest tests/test_acceptance.py tests/test_acceptance_git_reads.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py tests/test_cli.py -q -p no:cacheprovider --basetemp <scratch>/targeted
```

The skip is the existing host-restricted symlink case. This run includes all
535 existing acceptance cases and all 13 new regression cases. Ruff and
`git diff --check` passed; an AST comparison confirmed that the existing
acceptance test functions and parameterizations were unchanged.

No full-repository suite or CI wall-time guarantee is claimed. Further process
reduction remains a follow-up; it must preserve mutable-state and Git failure
checks rather than merely hide work from the counter.

## Template maintenance correction

The measurements above describe commit `5bb28bb`. A subsequent Linux CI run
found a template-copy race with Git's detached automatic maintenance. Template
setup now sets `gc.auto=0`, `maintenance.auto=false` and `gc.autoDetach=false`
before the first commit. The candidate inherits those settings before its
commit. Every setup command runs synchronously; automatic maintenance is never
started by those commits, so there is no detached writer to wait for before
copying. No lock files are ignored. This adds three config processes per session;
the earlier repeated timings have not been remeasured for this correction.

The correction also limits CLI caching to `close` commands. Its targeted run
passed **296 tests** (Git-read regressions, the CI-failing acceptance case,
schema-3 hygiene cases and CLI tests). New tests check both templates' settings
and inspect them before each commit. The checkout-root and enclosing-scope tests
each kill their corresponding in-memory mutant. Linux CI remains the check for
the original platform-specific race; the local run was on Windows/Python 3.10.
