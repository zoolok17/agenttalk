# Development gate reference

Audience: agenttalk contributors and CI integrators who need SHA-bound evidence for a candidate change.

`agenttalk dev-gate` is the single voting command for repository tests, packaging checks, and CLI-runnable
security checks. It has no skip flags. A missing interpreter, tool, result, or evidence field blocks the run.

## Prerequisites

Run the command from a clean Git worktree. The local profile invokes both direct interpreters, so provision the
gate dependencies in **both** CPython 3.10 and CPython 3.14 (a CI leg needs them only in that leg's interpreter):

```text
python3.10 -I -m pip install -r dev-gate-requirements.txt
python3.14 -I -m pip install -r dev-gate-requirements.txt
```

Install `gitleaks` on `PATH` before a local run or the canonical `linux/3.12` CI leg. The gate resolves the
binary to an absolute path, records its version, requires full Git history, and owns the scan arguments.

The local profile requires direct CPython 3.10 and 3.14 executables. Use `--python` when the executables are
not discoverable:

```text
agenttalk dev-gate --profile release \
  --python 3.10=/absolute/path/to/python3.10 \
  --python 3.14=/absolute/path/to/python3.14
```

On Windows, use the equivalent absolute `python.exe` paths. A version bump must be committed before the gate
runs so the candidate SHA and the tested package version describe the same revision.

## Command surface

```text
agenttalk dev-gate [--profile release]
                   [--ci-leg OS/PYTHON | --aggregate DIRECTORY]
                   [--evidence ABSOLUTE_PATH]
                   [--temp-root ABSOLUTE_DIRECTORY]
                   [--python MINOR=ABSOLUTE_EXE ...]
```

- With neither `--ci-leg` nor `--aggregate`, the command runs the local fast precheck on Python 3.10 and 3.14.
  Its artifact has `complete: true` for the local scope, but it is not the authoritative CI matrix decision.
- `--ci-leg` accepts one declared matrix member: Linux, Windows, or macOS on Python 3.10 through 3.13. A leg
  artifact always has `complete: false`; it cannot claim a full gate pass by itself.
- `--aggregate` reads leg artifacts recursively, rejects malformed, missing, duplicate, stale, or mixed-bound
  evidence, and compares the common SHA/tree/manifest binding with the current clean checkout. Only the exact
  12-leg set can produce `complete: true`.
- `--evidence` and `--temp-root` must resolve outside both the candidate worktree and `AGENTTALK_ROOT`. Defaults
  use the system temporary directory. Pytest basetemps are short children of that external run directory.

Exit status `0` means the requested scope passed. Status `1` means a complete execution produced blocking
check evidence. Status `2` means a preflight, schema, binding, or invocation error blocked execution; when an
external evidence path can be established, the command still writes a normalized
`agenttalk-dev-gate-preflight-block` artifact with `complete: false` and the stable blocker code.

## Committed plan

[`dev-gate.json`](../dev-gate.json) is the strict plan. The command reads its committed `HEAD` blob, not an
uncommitted working-tree copy, and records both its Git blob ID and SHA-256 digest. The runner also records a
logical plan digest, the candidate commit/tree, and the committed runner blob ID/digest. Before executing the plan,
the CLI re-enters a temporary committed Git export through isolated Python, so index flags and candidate-root
module shadows cannot make mutable checkout code masquerade as the attested runner. Every Python-backed tool is
resolved before the candidate import root is exposed. [`dev-gate-requirements.txt`](../dev-gate-requirements.txt) provisions tools only; it
does not own check selection or argv.

Every local run checks:

- full pytest in source mode and built-wheel mode on Python 3.10 and 3.14;
- one sdist and one wheel built without build isolation;
- sdist exclusion sentinels and required shipped files;
- dependency-resolving wheel installation in fresh `system_site_packages=False` runtime and test environments,
  using copied venv launchers and disabled pip configuration/cache, followed by `pip check`, package-version
  provenance, and byte-equal console CSS/JavaScript;
- Ruff, Bandit, full-history gitleaks with a Git-only child `PATH`, pip-audit over the frozen dependency snapshot
  without pip re-resolution, Semgrep, and zizmor;
- clean and stable Git binding before and after execution.

Every CI leg runs the source/wheel, packaging, and binding checks for its one interpreter. The canonical
`linux/3.12` leg additionally runs Ruff, Bandit, gitleaks, pip-audit, Semgrep, and zizmor. CodeQL remains the
single declared CI-native exception because GitHub owns its analysis runtime.

Wheel dependency/test-tool resolution, Semgrep registry rules, and the PyPI advisory database are live external
inputs. Their locators, observation times, and explicitly unversioned identities appear in `external_inputs`;
the evidence never presents them as content-addressed inputs.

## Evidence contract

Run artifacts use `artifact_type: agenttalk-dev-gate-run`. They contain exact required check IDs, commands,
resolved tool paths and versions, exit codes, durations, log hashes and tails, isolated-venv creator/prefix
proofs, pip-configuration and child-`PATH` isolation assertions, import provenance, package
artifact hashes, isolation assertions, blockers, and recomputed summary counts. The writer validates the full
schema before and after a normalized durable JSON write.

Aggregate artifacts use `artifact_type: agenttalk-dev-gate-aggregate`. Each leg entry binds the raw input
artifact SHA-256. A passing aggregate proves that all declared legs share the same candidate SHA, tree,
version, manifest blob/digest, and logical plan digest and that the current checkout still matches them.

CI uploads every leg artifact even when its gate command blocks. The always-run aggregate converts a crashed
or evidence-less leg into an incomplete blocking artifact instead of silently reducing the matrix.

## Release-candidate provenance and package custody

`.github/workflows/release-provenance.yml` is an explicit, read-only
`workflow_dispatch` for a post-bump release candidate. The operator selects
`master` and supplies both the full 40-character candidate SHA and stable
`X.Y.Z` version. Preflight requires the selected event SHA, checked-out HEAD,
and executing workflow SHA to equal that input; it also checks the package and
module versions, dated changelog heading, every install pin (including the
new-user manual), the new-user-manual and roadmap baselines,
assurance ledger entry, clean checkout, empty runtime dependency list, and
monotonic version against existing stable tags. The generated
`docs/AGENTTALK-NEW-USER-MANUAL.pdf` must be a non-empty regular file changed
somewhere since the latest stable tag. That guards against shipping a completely
unchanged PDF, but it does not prove semantic source/render parity; the operator
must inspect its rendered version and install pin before tagging.
Master advancing before the dispatch therefore produces
`release_evidence_sha_mismatch`; the workflow never retargets to the new tip.

The dispatch calls the same committed 12-leg workflow and CodeQL workflow at
that SHA. Only a fresh run attempt is accepted: GitHub partial reruns can reuse
an older successful job, so a rerun refuses as `release_evidence_stale` and the
operator starts a new dispatch. The whole dispatch also expires after 24 hours,
which bounds the age of the same-attempt CodeQL result even though GitHub owns
that analysis runtime. The gate's canonical `linux/3.12` leg copies its
exact wheel and sdist from the external gate temp root before runner cleanup.
The final job downloads all 12 raw leg records, the aggregate plus run-attempt
receipt, and those package bytes from the same run namespace; validates their
schema, SHA/tree/version, freshness, raw-byte digests, and exact set; then
rehashes the packages during the custody transfer.

One self-contained Actions artifact carries `release-provenance.json`,
`SHA256SUMS`, the exact wheel/sdist, all raw leg records, the aggregate, and its
receipt. Its 90-day retention is a requested ceiling, not a durability promise:
repository policy or deletion can shorten it. The upload action's carrier
digest is published in the run summary. Increment 2 must attach these exact
bytes to the eventual GitHub Release before expiry; it must not rebuild them.

### Verify a downloaded provenance artifact

Use the artifact id and `sha256:` carrier digest from the successful run
summary. This PowerShell procedure creates a fresh, plain verification
directory, downloads the original archive, verifies the carrier before
extraction, rejects reparse points, then requires the extracted file set to
equal the inner manifest exactly:

```powershell
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = gh repo view --json nameWithOwner --jq .nameWithOwner
$artifactId = '<artifact-id-from-run-summary>'
$expectedCarrier = '<sha256-digest-from-run-summary>' -replace '^sha256:', ''
$verificationRoot = Join-Path (Get-Location) ('.release-verify-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $verificationRoot | Out-Null
$rootInfo = Get-Item -Force -LiteralPath $verificationRoot
if (($rootInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw 'verification root is a reparse point'
}
$archive = Join-Path $verificationRoot 'release-provenance.zip'
$bundle = Join-Path $verificationRoot 'bundle'
New-Item -ItemType Directory -Path $bundle | Out-Null
$headers = @{
  Accept = 'application/vnd.github+json'
  Authorization = "Bearer $(gh auth token)"
  'X-GitHub-Api-Version' = '2022-11-28'
}
Invoke-WebRequest -Headers $headers `
  -Uri "https://api.github.com/repos/$repository/actions/artifacts/$artifactId/zip" `
  -OutFile $archive
$actualCarrier = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
if ($actualCarrier -ne $expectedCarrier.ToLowerInvariant()) { throw 'carrier digest mismatch' }
Expand-Archive -LiteralPath $archive -DestinationPath $bundle
$bundle = (Resolve-Path -LiteralPath $bundle).Path
$reparse = @(Get-ChildItem -Force -Recurse -LiteralPath $bundle | Where-Object {
  ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
})
if ($reparse.Count -ne 0) { throw 'artifact contains a reparse point' }
$expectedMembers = [Collections.Generic.Dictionary[string,string]]::new(
  [StringComparer]::Ordinal
)
Get-Content -LiteralPath (Join-Path $bundle SHA256SUMS) | ForEach-Object {
  if ($_ -notmatch '^([0-9a-f]{64})  (.+)$') { throw "invalid SHA256SUMS row: $_" }
  $expected = $Matches[1]
  $relative = $Matches[2]
  if ($expectedMembers.ContainsKey($relative)) { throw "duplicate manifest member: $relative" }
  $candidate = [IO.Path]::GetFullPath((Join-Path $bundle $relative))
  $prefix = $bundle + [IO.Path]::DirectorySeparatorChar
  if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "manifest path escapes bundle: $candidate"
  }
  $canonical = [IO.Path]::GetRelativePath($bundle, $candidate).Replace('\', '/')
  if ($canonical -cne $relative) { throw "manifest path is not canonical: $relative" }
  $memberInfo = Get-Item -Force -LiteralPath $candidate
  if ($memberInfo.PSIsContainer -or
      (($memberInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
    throw "manifest member is not a plain file: $relative"
  }
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw "member digest mismatch: $candidate" }
  $expectedMembers.Add($relative, $expected)
}
$actualMembers = @(
  Get-ChildItem -Force -File -Recurse -LiteralPath $bundle |
    ForEach-Object { [IO.Path]::GetRelativePath($bundle, $_.FullName).Replace('\', '/') } |
    Where-Object { $_ -cne 'SHA256SUMS' } |
    Sort-Object
)
$manifestMembers = @($expectedMembers.Keys | Sort-Object)
if (@(Compare-Object $manifestMembers $actualMembers -CaseSensitive).Count -ne 0) {
  throw 'artifact file set differs from SHA256SUMS'
}
Write-Host "verified release provenance in $bundle"
```

Finally inspect `release-provenance.json`, confirm its candidate SHA and version
match the intended tag, and confirm the artifact root contains the named wheel
and sdist. Open the shipped new-user PDF and confirm its rendered baseline and
install pin match the candidate. Do not tag from a missing download, a partial rerun, a different SHA, or
an artifact older than the workflow's 24-hour evidence window.

Evidence refusal reasons remain distinct and machine-readable:

- `release_evidence_missing` — a required record or package is absent;
- `release_evidence_stale` — evidence is too old or belongs to another dispatch/run attempt;
- `release_evidence_sha_mismatch` — evidence is bound to another candidate SHA or tree.

Corrupt schema/content, digest substitution, an incomplete/failed gate, and a
version mismatch have separate refusal codes as well. The workflow has no
repository-content, package, or release mutation permission; the reusable
CodeQL job alone receives its required scoped `security-events: write` plus
`actions: read`. It creates no tag, GitHub Release, or package publication.
Cancelling the dispatch prevents the new provenance job from assembling or
uploading an artifact (the pre-existing gate aggregate may still finish its
bounded diagnostic cleanup).
This project has one human operator, so the later manual publish action is a
temporal double-check rather than two-party control. There is deliberately no
username allowlist pretending otherwise.
