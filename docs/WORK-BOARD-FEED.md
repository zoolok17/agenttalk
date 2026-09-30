# Work-board feed reference

Audience: console integrators consuming the read-only bus-derived board.

`GET /api/work-board?root=<project-id>` selects a configured root using the same
rules as the other console feeds. Unknown roots return HTTP 400. The response is
JSON, with no cursor or history endpoint. It never publishes work or grants merge
authority. The shared snapshot worker computes the projection; polling does not
read message files, run Git, or query external CI.

Schema version 1 includes `target_root_project_id`, `generated_at`, `coverage`,
`items`, `legacy`, `unassigned`, `total_count`, `truncated`, `omitted_count`,
`errors`, and `window_days`. During initial discovery, coverage is `building` and
the total is unknown. After a refresh failure, available cards are `last_known`;
consumers must show degraded coverage and must not present these as current Ready.

The feed includes active items and Done items whose approximate last work event
falls within seven UTC days. Entirely cancelled/declined work without remaining
obligations is excluded only when its history is complete. Unknown items remain
visible. Display order uses event time and item key, not causal workflow order.
Done needs integration evidence recorded by the lead-run `agenttalk board
verify-merges` (below); task completion alone never means Done. External CI is
labelled `local checks not tracked`.

There are at most 100 tagged cards and 256 KiB of UTF-8 JSON, including groups and
coverage. Overflow omits whole cards, sets `truncated`, and reports known totals
and omissions; incomplete totals are null. One counted Legacy group and the
unassigned group sit outside the card count. Oversized group details can be omitted
with `groups_truncated`. A card too large to fit is counted as omitted.

Coverage budgets the selected history across active and compacted partitions,
including linked untagged threads, before applying the card limit: 50,000 envelopes
and 128 MiB source bytes. At 60% it warns to schedule archive indexing. Overflow
retains last-known placement with `capacity_exceeded`; it cannot certify a partial
reduction. Coverage also reports scan times, valid-until, generation and discovery
statistics. Freshness expires 15 seconds after scan start.

Local checks use `wb.<item>.c<cycle>.<check>` with check keys matching
`[a-z0-9-]{1,24}`, scope `<project-id>/<item>/c<cycle>`, and the exact full candidate
revision. Only validated blocker-green evidence satisfies a required check. Missing
revisions, wrong scopes, advisory greens and another item's gates cannot satisfy it.
Global barriers remain separately visible on cards and keep their ordinary effect
on `check --gates`.

Root checks, non-board scoped checks, and both attention feeds exclude `wb.` gates.
Adding these names to root `required_gates` is refused. Legacy root-list entries
are ignored with a configuration warning. Malformed gate state still fails closed.
Board-gate cleanup is separate future maintenance, never a GET side effect.

## Integration facts and the Done lane

A card reaches Done only when its reviewed candidate is proven merged into an
approved target. A lead-run command makes that proof outside the web server and
records it in a facts file. The snapshot worker reads the file and never runs
Git. This replaces the in-server cached observer of design section 4 (the
operator's decision of 2026-09-30). Section 4's alias allowlist and probe rules
still apply, inside the command.

### Setup

`work_repos` in the project config is an access allowlist of operator-approved
local checkouts, not an item registry. For this repository:

```json
"work_repos": {
  "agenttalk": {
    "path": "D:\\Projects\\Claude\\agenttalk",
    "targets": ["refs/heads/master"],
    "default": true
  }
}
```

- An alias is a lowercase slug.
- `path` must be the canonical, absolute top of a non-bare, non-shallow work
  tree: no `..`, symlink, junction or other spelling of it.
- `targets` are distinct full refs under `refs/heads/` or `refs/remotes/`. A
  short name such as `master` is refused, because a tag could shadow it.
- A dispatch may declare `work_repo` (an alias) and `work_target` (`master` or
  `refs/heads/master`). An item without a declaration uses the one alias marked
  `default`. With no default, an unlisted alias or conflicting declarations,
  the item stays Unknown. An explicitly empty `work_repo` or `work_target` is
  an invalid binding ("empty repository/target declaration"), never the same
  as no declaration. Paths never come from messages.
- Invalid entries, two aliases for one checkout, and two defaults are refused
  with a warning.

Each card carries `repo_binding`: `null`, `{repo, target}` or `ambiguous`.

### Command

`agenttalk board verify-merges [--dry-run] [--json]` reduces the board
read-only, over active and compacted envelopes. It checks each (work item,
candidate) pair against each approved target of the item's alias:

- Only `rev-parse`, `merge-base --is-ancestor` and `cat-file` run: shell-free,
  with validated arguments.
- Inherited `GIT_*` variables are dropped. `GIT_NO_LAZY_FETCH=1`,
  `GIT_NO_REPLACE_OBJECTS=1`, `GIT_TERMINAL_PROMPT=0`,
  `-c core.fsmonitor=false` and `--no-replace-objects` are set. No hooks,
  fetch, status, diff, filters or textconv are used.
- Probes run one at a time, with a 2 second timeout each. The first timeout
  abandons that checkout for the rest of the run.
- A target must resolve to exactly its configured ref, with no ambiguity
  warning.
- A shallow checkout, a missing candidate or a failed ancestry check records
  no fact (Unknown).

One run at a time: a publishing run holds `.agenttalk/work-board-verify.lock`
(outside `state/`, so a reset cannot remove it) from its first read to its
publish, so two runs never interleave. A second run is
refused at once with "another verify-merges run is in progress". A dry run
publishes nothing and takes no lock. The snapshot worker never takes this lock.

The command lists unknown pairs. It exits 2, having published nothing, when:

- no usable alias is configured;
- another run is in progress;
- the store session changed during the run (a reset);
- the facts file has an unsupported schema version (the file is left untouched).

Otherwise it exits 0.

### Facts file

`.agenttalk/state/work-board-facts.json` (schema version 1) is replaced
atomically under `state/work-board-facts.lock`; other sections of the file are
kept. It holds at most 400 facts and 512 KiB. Each fact records `project`,
`repo_alias`, `repo_path`, `work_item`, `candidate`, `target_ref`, `target_oid`
(at check time), `checked_at` and `result` (`integrated` or `not_integrated`).
Each run rewrites the integration section for the current pairs, so an
invalidated result disappears. The section records the store `session_id` its
run started in. The run publishes only if that session is still current, and
the reader ignores a section from another session, with a warning.

The snapshot worker reads the file on every refresh, bounded and schema-checked:

- A missing, unreadable, malformed or oversize file gives no facts and a warning
  in `errors`. A missing file is silent only while `work_repos` is not
  configured. Any other fault while reading or selecting the evidence also
  becomes a warning. The facts never error the board and never imply Done.
- A fact whose project, alias path or target no longer matches the config is
  ignored, with a warning, so old proof is never reused after a remap.
- A fact counts only for the item's CURRENT binding: its declared `work_repo`
  and `work_target` (from its current cycle), else the current default alias
  and its targets. Proof from an earlier default or an earlier target is never
  reused. Unmatched facts leave the card's column as derived and end its reason
  with `integration evidence ignored: <why>`.
- A fact older than `integration_facts_max_age_seconds` (default 86,400; from
  60 seconds to 30 days), or dated after the reader's clock (writer and reader
  run on one host), is stale. It never makes Done: the card keeps its
  derived column, its reason ends with `integration evidence stale (as of
  <time>)`, and `integration_stale_as_of` holds the time.
- A fresh `integrated` fact for the current candidate places the card in Done,
  subject to every Ready condition. The card's `integration` maps the candidate
  to `{repo_alias, target_ref, target_oid, checked_at}`. A fresh
  `not_integrated` fact maps it to `false`.

### Refresh owners

The lead runs `agenttalk board verify-merges` right after every merge and on
every lead tick. Nothing else refreshes the facts, so without it Done goes stale
within the window.

Ordinary merges and fast-forwards are proven by ancestry. Squash and rebase
merges stay unproved.
