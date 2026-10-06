# Machine budget feed reference

For dashboard integrators who need this machine's recorded spending figures.

## In plain words

The dashboard can supply budget figures from the gateway ledger on the same
machine. You must turn this on when starting the dashboard; otherwise it does
not read the ledger and the extra address is unavailable. These figures are
not the provider's bill and do not include other machines. This is a data feed
only: it adds no budget screen and cannot change a limit or release a hold.

## Availability

`agenttalk dashboard --enable-budget` and `agenttalk serve --enable-budget`
enable `GET /api/budget`. The flag defaults to false and is independent of
`--enable-actions`. The `start` command does not enable this feed. Existing
routes and their answer shapes are unchanged with either flag setting.

The endpoint follows the dashboard's existing loopback-only binding and peer
checks. It describes the machine running the server, not the selected team:
there is one feed even when several project roots are displayed. Query
parameters do not select another ledger, project or machine. It uses the same
default ledger and install-marker locations as `agenttalk gateway status`.

Without the flag, `/api/budget` returns HTTP 404. With the flag, every expected
read outcome is an HTTP 200 JSON answer with the following common fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `1`. |
| `status` | `ok`, `not_set_up`, `busy`, or `unavailable`. |
| `coverage` | “These are this machine's ledger figures, not the provider's bill. Other machines are not included.” |
| `observed_at` | UTC time at the start of this read attempt. |
| `age_seconds` | Whole seconds since that attempt finished, measured with a monotonic clock. |
| `cache_seconds` | `10`: minimum reuse time after a completed attempt. |

`not_set_up` means both the ledger file and install marker are absent; the feed
does not create them. If only one exists, the installation is incomplete and
the answer is `unavailable`.
`busy` means the read met a database lock, exceeded its deadline, or another
budget read is already running. Its message is “Busy, try again.”
`unavailable` means the figures could not be trusted or read, for example an
unreadable file, an incomplete installation, or failed snapshot validation.
These three states include a plain `message` and **no money or hold fields**;
missing figures must not be displayed as zero. No exception detail or file
path is returned.

## Successful answer

An `ok` answer adds these fields. Money is an integer number of micro-euros:
1,000,000 means EUR 1. No floating-point rounding is involved in the answer.

| Field | Meaning |
| --- | --- |
| `month` | UTC calendar month of the observation, `YYYY-MM`. |
| `committed_micro_eur` | That month's recorded charged costs, plus the opening balance only in its opening month. A month without a ledger row has zero committed. |
| `opening_micro_eur` | Opening balance recorded when this ledger was initialized, including after a reset. |
| `opening_period` | Month to which that opening balance belongs. It remains visible in later months but is not added to their spending. |
| `soft_stop_micro_eur` | This ledger's chosen alert threshold. It is not itself an instruction to stop. |
| `trial_cutoff_micro_eur` | This ledger's chosen trial cutoff. Admission accounts separately for the opening allowance and outstanding reservations. |
| `external_ceiling_micro_eur` | The ledger's outside ceiling, checked against cumulative committed spending and reservations, not just this month's figure. |
| `service_hold` | Whether the ledger records a durable accounting hold. Its private reason is omitted. |
| `unresolved_attempts` | Count of reserved or uncertain attempts across all months. These also block new transport, even when `service_hold` is false. |

There is no total for money held by unresolved reservations: the gateway's
status snapshot supplies individual reservation amounts, not that total.
This feed does not calculate a separate total or expose those individual records.

The committed figure has the same definition as
[the gateway report](QWEN-OVH-TRIAL.md#the-ledger-report-report-version-1).
An unresolved reservation without a recorded charge is not committed money;
an uncertain attempt with an already recorded charge is included. The answer
contains no agent names, message or request identifiers, credentials, token
counts, or paths.

An `ok` result means the figures were readable, **not permission to spend**.
Budget limits, unresolved attempts, and other gateway checks still apply.
Consumers should show the observation time and handle cached figures as a
recent observation rather than a live provider total. A reset, hold or month
change may take the cache interval to appear.

## Read limits

The dashboard opens SQLite with `mode=ro` and zero busy timeout. It never uses
the gateway's write-capable connection setup, migrates the database, changes
its journal mode, or creates a missing ledger. Only rollback-journal ledgers
(the gateway's normal `PERSIST` mode) are supported; WAL files are refused so
a read cannot create shared-memory sidecars. There is no `immutable` or
`nolock` shortcut.

The existing status snapshot logic runs inside one read transaction, then the
feed selects only the fields above. SQLite's progress handler cancels queries
at 150 ms. A separate reader process has a 500 ms outer timeout, including
interpreter startup and Python work between queries; after timeout the server
terminates and reaps it before answering. Normal completion, errors and query
timeouts roll back and close the connection in `finally`. A forced stop
releases the process's file handles and locks through the operating system.
Process creation, termination and OS scheduling are not real-time guarantees.

These reads can briefly delay a gateway commit, which normally waits up to
five seconds for locks. Each dashboard server keeps one result for at least
ten seconds, including failed reads; concurrent requests cannot start another
reader. No reader process is retained after an answer, and no background
refresh loop runs. HTTP responses retain `Cache-Control: no-store`; the
ten-second cache belongs to the server, not the browser.
