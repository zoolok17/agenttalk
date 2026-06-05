# Quickstart / Validation Scenarios: Trusted-Team Safety 0.16.0

Each scenario maps to a spec primary flow and is the manual/automated
acceptance check for the release. Commands shown in PowerShell.

## Prereq
```powershell
pip install -e .        # tests run against the installed package, not src/
pytest -q               # baseline green before starting
```

## S1 — Mark a clean epoch boundary, then auto-stamp openers (FR-009..011)
```powershell
agenttalk barrier bump --from claude --scope global -m "voiding the previous run"
# -> prints epoch id E1 (the barrier message id)
agenttalk send --from claude --to codex --kind review-request --subject x -m "review WP" --meta request_id=r1
# the r1 opener now carries meta.epoch_at_send == E1 (verify via recv/transcript)
```
**Pass**: barrier appears as an ordinary message with `meta.barrier`; its id is
the epoch; the new opener's `meta.epoch_at_send` equals that id.

## S2 — Cheap currentness check before acting (FR-012, FR-013)
```powershell
agenttalk check --for codex --to-request r1 --epoch         # -> current, exit 0
agenttalk barrier bump --from codex --scope global -m "new epoch"   # epoch E2
agenttalk check --for codex --to-request r1 --epoch         # -> previous-epoch, exit 3
```
**Pass**: r1 is current under E1, becomes previous-epoch (exit 3) once E2 fires.
With no barrier ever fired, `check --epoch` on any request reports current.

## S3 — Safe rename (FR-005, FR-006)
```powershell
agenttalk roster rename codex codex-rev --drain-check
# if work is owed to/from codex: refused (exit 2), lists owed threads
# else: codex retired (renamed_to=codex-rev), codex-rev now active
agenttalk recv --for codex-rev          # historical codex messages still validate
agenttalk send --from codex ...          # refused: codex is a tombstone (exit 2)
```
**Pass**: history referencing `codex` stays valid; `codex` can't send; `codex`
can never be re-bound; role/group/liaison bits carried to `codex-rev`.

## S4 — Retire a departing agent (FR-002, FR-003, FR-004)
```powershell
agenttalk roster retire codex --reason "leaving the band"
agenttalk send --from codex --to claude -m hi      # refused, exit 2 (tombstone)
agenttalk roster retire codex                       # refused, exit 2 (already retired)
```
**Pass**: tombstone recorded; retired identity cannot send; history intact.

## S5 — Remove is refused with a hint; force overrides (FR-007)
```powershell
agenttalk roster remove codex            # refused, exit 2, hint -> use `roster retire`
agenttalk roster remove codex --force    # proceeds, WARNS about history-read breakage
```
**Pass**: bare remove refused with the retire hint; `--force` removes and warns;
no tombstone created by force (name remains re-addable).

## S6 — Single-hop retired forwarding (FR-008)
```powershell
agenttalk roster retire codex
agenttalk roster forward codex --to claude --reason "codex left; route to claude"
# emits a transcript-visible note with meta.forward={from_retired:codex,to:claude,hop:1}
agenttalk roster forward claude --to codex-rev   # refused: claude is active, not a tombstone
```
**Pass**: a single forwarding hop is recorded with auditable meta; forwarding
from an active identity or a second hop is refused.

## S7 — Tooling sees who owes the next move (FR-014, FR-015)
```powershell
agenttalk threads --for claude --json   # open rows carry next_owner / next_action
agenttalk sync --for claude --json      # same fields where derivable
```
**Pass**: an `owed-inbound` thread shows `next_action:"reply"`,
`next_owner:"claude"`; a `reply-waiting` thread shows `next_action:"await-reply"`,
`next_owner:<peer>`; terminal threads omit both; no `send`/CLI input can set
them; delivery/unread/closure are unaffected.

## S8 — Backward compatibility (NFR-002, SC-005)
```powershell
# Against a store created by 0.15.0 with no retired/barriers/epoch openers:
pytest -q                                # all existing tests stay green
agenttalk threads --for claude --json    # no next_* keys appear where not derivable
agenttalk check --for claude --to-request <old-rid>   # unchanged (no --epoch) behavior
```
**Pass**: zero behavior change until the new commands are used; old messages and
JSON shapes validate and render unchanged.

## Release gate
```powershell
pytest -q                 # full suite green locally
# then: push, watch the CI matrix (py3.10-3.13 x 3 OSes) to GREEN before tagging
gh run watch
```
**Pass**: full CI matrix green (NFR-005) is mandatory before any 0.16.0 tag.
