# CLI Surface Contracts: Trusted-Team Safety 0.16.0

New and changed command surfaces. All are additive; existing invocations are
unchanged. Exit codes follow the established contract (0 ok, 1 wait-timeout, 2
usage/refusal, 3 superseded/stale, 4 unknown rid, 5 partial fan-out, 130 SIGINT).

## roster retire

```
agenttalk roster retire <name> [--reason <text>]
```
- Transitions an active identity to a permanent retired tombstone in
  `config.json` (`renamed_to: null`).
- Refuses (exit 2) if `<name>` is not active (already retired, or unknown).
- Does NOT touch message files; `<name>`'s history stays valid.
- Output: confirmation naming the tombstone; `--json` returns the updated
  registry slice `{"retired": [...]}`.

## roster rename

```
agenttalk roster rename <old> <new> [--drain-check] [--reason <text>]
```
- Retires `<old>` (tombstone with `renamed_to: <new>`) and registers `<new>` as
  a new active identity, in one atomic `config.json` write.
- `--drain-check`: refuse (exit 2) if any open thread owes work to/from `<old>`
  (derived from `threads`); lists what is owed. Without `--drain-check`, rename
  proceeds regardless (operator's call).
- Refuses (exit 2) if `<new>` is already active OR is a retired tombstone
  (non-rebindable), or if `<old>` is not active.
- `<old>`'s historical messages remain valid (validated against the known
  roster).
- Carries over `<old>`'s role / group memberships / `operator_facing` to `<new>`
  (so a rename doesn't silently drop the liaison bit). Documented in output.

## roster remove

```
agenttalk roster remove <name> [--force]
```
- WITHOUT `--force`: refuse (exit 2) with a hint:
  "removing <name> breaks historical readability for its messages; use
  `roster retire <name>` to keep history valid, or pass --force to remove
  anyway."
- WITH `--force`: remove `<name>` from `agents`/`roles`/`groups` as in 0.15.0
  `remove_agent`, and print a WARNING that messages from `<name>` will now fail
  roster validation (historical-read breakage knowingly accepted).
- `--force` does NOT create a tombstone (it's a true removal); the name therefore
  remains re-addable. This is the documented escape hatch, distinct from retire.

## roster forward (optional, single-hop retired forwarding)

```
agenttalk roster forward <retired-name> --to <live-agent> [--reason <text>]
```
- Records, in transcript-visible meta, that an outstanding obligation owed to a
  retired identity is redirected to a live agent — a single explicit hop.
- Emits an ordinary message (`kind=note`) from the operator/liaison context with
  `meta.forward={"from_retired": <retired-name>, "to": <live-agent>,
  "hop": 1}` so the redirect is auditable.
- Refuses (exit 2) if `<retired-name>` is not a retired tombstone, if `<to>` is
  not active, or if the source is itself being forwarded (multi-hop: a message
  whose own opener already carries `meta.forward` cannot be forwarded again).

## barrier bump

```
agenttalk barrier bump --from <agent> --scope global -m "<reason>"
```
- Fires a single ordinary message (`kind=message`, self-addressed) carrying
  `meta.barrier={"version":1,"scope":"global","type":"epoch-bump"}`.
- Prints the new epoch id (= the barrier's message id). `--json` returns
  `{"epoch": "<id>", "scope": "global"}`.
- `--from` must be an ACTIVE roster member (retired identities cannot bump —
  they cannot send). `--scope` accepts only `global` in 0.16.0 (other values
  refused, exit 2 — scopes are reserved, not a workflow engine).
- Any active member may bump (documented global-stall lever; trusted-team only).

## check --epoch

```
agenttalk check --for <agent> --to-request <rid> --epoch [--json]
```
- Extends the existing `check` (#12). Without `--epoch`: unchanged (rescind-only,
  exit 0/3/4). With `--epoch`: ALSO compares the request's `epoch_at_send` to
  `current_epoch()`.
- Exit codes (see data-model §4): 0 current, 3 superseded-OR-previous-epoch,
  4 unknown. Human/JSON text distinguishes rescinded vs previous-epoch vs
  unknown-pre-epoch.
- Read-only: no cursor/heartbeat/threadstate writes (same as 0.14.0 `check`).

## threads --json / sync --json (changed output)

```
agenttalk threads --for <agent> --json
agenttalk sync --for <agent> --json
```
- Each open thread row MAY now include `next_owner` and `next_action`
  (controlled vocabulary; see data-model §5). Omitted on terminal threads and
  where not derivable.
- Pre-existing keys are unchanged; consumers that ignore the new keys are
  unaffected. Human (non-JSON) output may show a compact `next:` hint but MUST
  NOT change existing column meanings.

## Backward-compatibility contract (applies to all)

- A store with no `retired`, no barrier messages, and no epoch-aware openers
  behaves byte-for-byte like 0.15.0 for every existing command.
- New `meta` keys (`barrier`, `epoch_at_send`, `forward`) on a message are
  ignored by 0.15.0 readers (meta is free-form) — forward-compatible.
- No existing exit code changes meaning; no existing JSON key is removed or
  retyped.
