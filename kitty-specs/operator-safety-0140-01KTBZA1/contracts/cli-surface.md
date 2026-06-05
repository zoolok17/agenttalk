# CLI Surface Contract: 0.14.0 Operator Safety

The complete externally-visible surface this release adds or changes.
agenttalk is a CLI, so this is the contracts/ artifact (no HTTP API).
Everything not listed here is unchanged. All global options
(`--root`, etc.) keep preceding the subcommand.

## New subcommands

### `agenttalk rescind --from <A> --to-request <RID> [--to-id <MSG>] [-m <reason> | --file <path|->] [--quiet]`

Marks request RID as no-longer-current via a transcript-visible message.

- Exit 0: rescind written (prints the message render unless `--quiet`).
- Exit 2: RID not found among A's outbound openers; A is not the
  requester of RID; malformed args. stderr names the problem + hint.
- Idempotent in effect: re-rescinding an already-superseded thread
  writes the message (audit) but state stays `closed-superseded`.

### `agenttalk check --for <A> --to-request <RID> [--json]`

The pre-action currentness gate. Read-only; never touches cursors.

- Exit 0 + `current`: no valid rescind newer than opener/target.
- Exit 3 + `superseded`: valid rescind found (prints its id, sender,
  timestamp, reason).
- Exit 4 + `unknown`: RID not found in valid messages visible to A.
- `--json`: `{"request_id": ..., "state": "current|superseded|unknown",
  "rescind": {...}|null}`.
- Documented contract: agents run this immediately before any
  irreversible action tied to a request.

### `agenttalk escalate --from <A> [-m <question> | --file <path|->] [--to <agent>] [--meta k=v ...] [--quiet]`

Routes an operator-input request to the liaison.

- Resolves recipient from config `operator_facing` unless `--to` is
  given explicitly.
- Mints `meta.request_id` (`esc-` prefix) unless supplied; always sets
  `meta.needs_operator=true`; sends `kind=question`; prints the
  request_id (machine-parseable line) so the caller can
  `wait --to-request` on it.
- Exit 2 + hint: no operator_facing configured (and no `--to`);
  designated agent missing from roster; sender is the liaison itself.

## Changed subcommands

### `agenttalk wait --for <A> --to-request <RID> ...`

- New outcome: if RID becomes superseded while waiting (or already is at
  wait start), wake immediately, print the rescind render with a
  `RESCINDED` banner, exit **3**. Exit 1 stays timeout-only; exit 0
  stays reply-delivered.

### `agenttalk init [--force]`

- Preflight: resolve upward from the target's parent directory; if an
  existing `.agenttalk/` store is found, exit 2 naming that store and
  advising `--root <found>` (join it) or `--force` (deliberate nested
  store).

### `agenttalk roster set-operator-facing <agent>` / `agenttalk roster set-operator-facing --clear`

- Sets/clears the single `operator_facing` config slot. Exit 2 if the
  agent is not in the roster. `roster` listing shows the marker
  (e.g. `lead  role=lead  groups=[...]  [operator-facing]`).

### `agenttalk composing --to-request <RID> ...` *(#14, conditional)*

- Sugar: sets `meta.request_id=RID` (validates RID is an open inbound
  thread for the sender; exit 2 otherwise) and records the
  reply-in-flight marker entry.

### `agenttalk whoami` / `agenttalk doctor`

- First output line is `root: <resolved root>`.
- `doctor` adds: multi-store detection (lists every store from CWD to
  filesystem root when >1 found, with a split-brain warning) and
  liaison diagnostics (operator_facing unset / not in roster / stale
  heartbeat).
- `whoami` shows `operator-facing: yes|no (liaison: <name|none>)`.

### `agenttalk status` / `sync` / `threads`

- `threads`: rows may show `closed-superseded`; liaison sees an
  `operator-input needed` bucket; reply-in-flight annotation (#14).
- `sync`: rescinded threads flagged in actionable/terminal sections;
  liaison digest gets an `escalations` section with deterministic
  next-action hints.
- `status`: warnings gain stale-pending-escalation and (when detectable
  from CWD) multi-store entries; per-agent line marks the liaison.

## Environment

### `AGENTTALK_ROOT` (new)

- Root resolution precedence: `--root` flag > `AGENTTALK_ROOT` > upward
  walk from CWD. Resolved root always printed first by
  `whoami`/`doctor`. Invalid env value (no store there) behaves exactly
  like an invalid `--root`: loud exit 2, never a fallback to the walk.

## Exit-code contract (global)

`0` ok/current · `1` wait timeout (exclusive) · `2` usage/refusal ·
`3` superseded/rescinded · `4` unknown request id · `130` SIGINT.
No existing code is repurposed (C-005).

## Backward compatibility

- All JSON additions are new keys/values only; existing keys keep their
  types and meanings (NFR-001).
- A pre-0.14.0 agenttalk reading the same store treats `rescind`
  messages as ordinary inbox content (transcript-visible by design) and
  ignores `operator_facing`, `needs_operator`, and the marker file.
- New state file `<agent>.composing.json` and config key
  `operator_facing` are ignored by older readers; corrupt instances
  degrade to current behavior.

## Skill-contract deltas (shipped with the release, both CLIs)

- Lead/liaison: single-voice rule — surface pending escalations to the
  human with context; answer on the same request_id; never relay raw
  noise.
- Workers: with a configured liaison, never ask your own window's
  human — `agenttalk escalate` instead; fall back (documented) only
  when escalate refuses.
- Everyone: run `agenttalk check --to-request <RID>` immediately before
  any irreversible action tied to a request; treat exit 3 as a hard
  stop + report-back.
- Requesters: prefer `rescind` over prose cancellations ("ignore my
  last message" does not move thread state).
