# Envelope service readers — the service layer bound to the DEFAULT envelope

Branch `fix/envelope-service-readers`, base `master` at `d24e72f` (v0.91.0). Field finding
2026-09-24: a gateway initialised with a non-default envelope (`--opening-eur 8.91 --cutoff-eur 50
--soft-stop-eur 45 --ceiling-eur 60`) had `gateway init` return the ledger's envelope-specific
`price_policy_hash`, while `gateway task-install` recorded the DEFAULT-envelope hash in the task
identity and the install manifest also held the default. Every service-layer site agreed with the
others (all default), so the gateway reported ready, and the ledger still enforced the real envelope
on reservations — but the manifest, task identity and runtime marker were no longer bound to the
ledger's actual policy, and `gateway status` reported a hash that was not the ledger's.

## Root cause

`12cc78a` (envelope at init) made `price_policy_hash()` / `child_cap_policy_hash()` take the envelope
as keyword arguments defaulting to the module constants, then enumerated and moved every reader in
`ovh_gateway.py` to the ledger's stored envelope. It did not enumerate `ovh_gateway_service.py`: those
sites kept calling the two functions with no arguments, which silently resolves to the default
envelope. Nothing failed, because writer and readers were wrong the same way. The `12cc78a` record
and its cold review covered the ledger module only; the service module was outside both.

## The fix

One authority, read the same way everywhere:

- `SpendLedger.policy_hashes()` (new, `ovh_gateway.py`) returns the ledger's own verified
  `price_policy_hash` and `child_cap_policy_hash` (the latter `None` until child caps are installed).
  It goes through `_connect()`, so the install marker, the metadata, the stored envelope and the
  recomputed hashes have all been cross-checked first; it is the same value `gateway init` returns and
  `SpendLedger.status()["policy_hash"]` reports.
- Every service-layer site takes its hash from that source (a `ledger=` argument, defaulting to
  `SpendLedger()` exactly as `run_service`/`start_task`/`gateway_status` already did). No service-layer
  code calls `price_policy_hash()` or `child_cap_policy_hash()` any more; the two imports were removed
  so a regression cannot compile silently.
- A manifest or task identity written under one envelope now fails its check against a ledger with
  another, because the expected value is the ledger's, not a constant.

## Enumerated readers — `src/agenttalk/ovh_gateway_service.py`

Grepped `price_policy_hash` / `child_cap_policy_hash` over `src/agenttalk/` (line numbers at `d24e72f`).
Every hit is classified; the first eight are the no-argument default-envelope calls.

| # | Site (at d24e72f) | Role | Now |
|---|---|---|---|
| 1 | `expected_task_identity` (~230) | task identity `price_policy_hash`, persisted to `task-identity.json` and compared in status | `_task_identity(..., policy_hash=ledger hash)`; `expected_task_identity(..., ledger=)` |
| 2 | `_initialize_install_locked` (~966) | install manifest written at `gateway init` | the marker `ledger.initialize()` returned (the value `init` prints) |
| 3 | `_reconfigure_endpoint_locked` (~1036) | pre-write manifest price-policy check | ledger hash; still refuses before any write |
| 4 | `_validate_install_manifest` (~1103) | manifest check on every load (`load_install_manifest`, rebind snapshot, rebind candidate) | ledger hash, via a new `ledger=` argument threaded through `_read_install_manifest*` |
| 5 | `_runtime_projection` expected (~1379) | runtime marker `price_policy_hash` check | caller-supplied ledger hash |
| 6 | `_runtime_projection` expected (~1380) | runtime marker `child_cap_policy_hash` check | caller-supplied ledger hash (`None` if caps not installed) |
| 7 | `run_service` marker write (~1624) | runtime marker `price_policy_hash` written at startup | `ledger_status["policy_hash"]` (already fetched and verified at startup) |
| 8 | `run_service` marker write (~1625) | runtime marker `child_cap_policy_hash` | `ledger_status["child_cap_policy_hash"]` (`child_cap_ready` is required, so non-`None`) |
| 9 | `gateway_status` (~1808) | top-level `price_policy_hash` | `None` until `ledger.status()` succeeds, then the ledger's `policy_hash` |
| 10 | `gateway_status` (~1809) | top-level `child_cap_policy_hash` | `None` until `ledger.status()` succeeds, then the ledger's value |

Not hash computations, but they carry the value and were traced:

- `_runtime_projection` output keys (~1405-1406) and `rebind_runtime`'s return (~1306, `next_manifest
  ["price_policy_hash"]`) pass through values already validated against the ledger; unchanged.
- `TaskIdentity.price_policy_hash` is **not** rendered into the task XML or the systemd unit, so
  `task_xml_matches` / `systemd_unit_matches` never read it. It matters only through
  `task-identity.json` vs `gateway_status`.

### Callers of the changed functions

- `expected_task_identity`: `install_task` (both platforms), `start_task`, `gateway_status`, tests.
- `stop_task` used to call it and now calls the new `registration_identity()`. Stop needs the identity
  only for `_query_registration` / `_registration_matches`, neither of which reads the hash, and
  operator stop must keep working when the ledger is blocked or absent (it is the kill path).
  `registration_identity` carries an empty hash and is documented never to be persisted or compared.
- `load_install_manifest` / `_read_install_manifest*`: `_reconfigure_endpoint_locked`,
  `_rebind_runtime_locked` (snapshot, candidate validation, post-write reload), `run_service`,
  `start_task`, `gateway_status`, and the CLI (`gateway task-install`).
- `cmd_gateway` (`cli.py`) needed no change: it calls the service functions without a ledger, so the
  default `SpendLedger()` is used, the same ledger `init` created. `wrap`'s `gateway_status(store.root)`
  is likewise unchanged.

### Outside the service module

- `policy_summary()` in `ovh_gateway.py` still uses the defaults. It has no caller anywhere in `src/`
  or `tests/`; recorded, left alone (dead code, not a service-layer reader).
- Everything else in `ovh_gateway.py` was moved by `12cc78a` and is unchanged here.

## Behaviour changes, stated plainly

- `gateway status` with an unavailable ledger now reports `price_policy_hash: null` and
  `child_cap_policy_hash: null` rather than a default-envelope hash that was not the ledger's. It also
  cannot validate the manifest or task identity without the ledger, so it adds `install_manifest_invalid`
  and `task_identity_invalid` alongside the existing `ledger_blocked`.
- `rebind_runtime`, `reconfigure_endpoint`, `install_task` and `load_install_manifest` gained an optional
  `ledger=` keyword and now read the ledger. An install whose manifest disagrees with the ledger's
  envelope is refused (`gateway install manifest price policy mismatch`) instead of accepted.
- For a default-envelope install nothing changes: the ledger's hash equals the module default, so the
  manifest, task identity, runtime marker and status are byte-identical to before.
- **Existing installs made at a non-default envelope on v0.91.0 already hold a default-envelope hash in
  `install-manifest.json` and `task-identity.json`.** After this fix those fail the manifest check
  (`install_manifest_invalid`) until the manifest is rewritten. This change does not include a migration;
  it is called out for the lead's decision (see the reply). The same install's running gateway also holds
  a runtime marker written with the default hash, so `gateway status` reports `runtime_marker_invalid`
  until the service is restarted on the fixed code.

## Tests

`tests/test_ovh_gateway_service.py`:

- `test_non_default_envelope_binds_the_ledger_hash_in_every_service_artifact` — install at a 40/36/60
  envelope; asserts the ledger hash (not the default) in the manifest, `task-identity.json`, the runtime
  marker `run_service` actually writes, and `gateway status` (top level and `runtime`); a marker carrying
  the default hash is rejected.
- `test_default_envelope_install_is_byte_identical_to_the_module_defaults` — same walk at the default
  envelope; every artifact equals the module-default hash.
- `test_manifest_and_task_written_under_one_envelope_fail_against_another_ledger` — a default-envelope
  manifest/task against a non-default ledger is refused by `load_install_manifest`,
  `reconfigure_endpoint` and `rebind_runtime`, the latter two leaving every file under the project root
  unchanged (with the pinned endpoint patched so a late refusal would have rewritten the config), and
  `gateway status` flags manifest and task identity.
- `test_status_reports_no_policy_hash_when_the_ledger_is_unavailable`.

Existing tests: those that installed or rebound without a ledger now pass one (or the
`_DEFAULT_LEDGER` stand-in for pure registration tests); the stop tests use `registration_identity`. An
autouse fixture points `LOCALAPPDATA` at a temp directory so a bare `SpendLedger()` in a test can never
resolve to the host's real ledger. `tests/test_ovh_gateway_lifecycle_integration.py` and one
`test_ovh_gateway_cli.py` case were moved to a ledger at the default path under a temp `LOCALAPPDATA`
for the same reason.

### Mutation verification

Each service-layer site was reverted, one at a time, to the default-envelope hash and the four new tests
run: init manifest, task identity, manifest validate, reconfigure raw check, runtime marker price,
runtime marker child-cap, status price, status child-cap. All eight were killed. (The reconfigure raw
check survived until the refusal test began asserting the project files are unchanged, because the
post-write reload raised the same message after the write.)

## Sweep

Confidentiality sweep (`grep -riE '<protected-1>|<protected-2>'`, the two protected strings of the local
confidentiality rule, named by placeholder only) over the changed and new files: no matches.
