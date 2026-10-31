"""The experiment recorder: durable, per-process journals of what a pytest run did (audit step P3).

The recorder is a pytest plugin that exists only on request.  `tests/conftest.py` calls `add_option` and
`configure`; with `--experiment-record=DIR` absent (and no nested-run handoff, below) `configure` registers
nothing, so there are no hooks and no files.  The reducer, `reduce_journals`, reads the journals back and checks
them against the serial collection.  JUnit XML and `user_properties` are a convenience only, never the evidence.

Write the option as `--experiment-record=DIR`, one argument.  Given as two arguments, the folder looks like a path
argument once it exists, pytest counts it when it looks for the rootdir (when no ini file fixes it), the workers
get another rootdir than the controller, and their node ids no longer match the serial collection's.

Security note (docs/SECURE-CODING.md).  Boundaries: the folder named on the command line or in the nested handoff;
node ids, skip reasons and error text from the tests; the journal files the reducer reads.
S1: a node id or an error text is data.  S3: the folder is resolved and must not lie inside any basetemp (pytest would
delete it); a journal is created exclusively (never reusing or following an existing name) and checked on the opened
file; the reducer opens without waiting and checks on the opened file that it is a plain file with one name.
S5: every record is one JSON line (`json.dumps` escapes quotes and control characters, so no text can end a line or
forge a record); U+2028 and similar stay literal, which is why a reader splits on "\\n" only.
S6: stored free text is cleaned of values the recorder can recognise (see "Free text and what is blanked out"),
including a long value that pytest shows shortened as 'head...tail'; no secret is written by the recorder itself.
S7: every refusal says which input was bad; nothing is swallowed.
S8: error text is cut at 64 KiB, reasons at 4 KiB, the handoff is limited to 4 KiB and a journal read to 64 MiB; a
refusal closes what it opened.  S9: JSON only.

Journal files
-------------
One journal per process, `journal-<role>-<pid>-<started_ms>.jsonl` in the record folder.  `role` is `serial` (no
xdist), `controller` (the xdist controller), `worker` (an xdist worker) or `nested` (the main process of a nested
pytest run that was started through `nested_environment`).  `started_ms` is the wall time, in milliseconds, at which
the recorder started in that process (the nearest portable stand-in for the process start time); together with the
pid it keeps a reused pid from colliding.  The file is created exclusively, held open for appending by one handle,
and `os.fsync` is called on it after every line.  A process that dies loses at most the line in flight; a torn last
line is possible and the reducer reports it.  The claim stops there: a killed process proves the records survive the
process, not a power or storage failure.

Each line is one JSON object, UTF-8, `ensure_ascii=False`, keys sorted.  Read a journal by splitting on "\\n" only
(U+2028 can appear inside a string).  A lone surrogate is written as a `\\udXXX` escape.

Common fields of every record
-----------------------------
v                 schema version, 2 (SCHEMA_VERSION).  Version 1 had no collection manifest and no identity on the
                  phases; the reducer refuses a record of another version (`bad-field`).  The nested handoff has
                  its own version, 1 (HANDOFF_VERSION), which did not change.
seq               1, 2, 3, ... with no gap, per journal.
kind              the event kind, below.
role, worker      the process role; the xdist worker id (`gw0`) for a worker, else null.
pid               the process id.
t_wall, t_mono    `time.time()` and `time.monotonic()` when the record was written.

Event kinds and their fields
----------------------------
process-start     first record.  started_ms (int); python, pytest (version strings); dist (the `--dist` value) and
                  loadgroup (bool: group suffixes are expected in node ids); plugins (sorted list of the names of
                  the plugins loaded in this process, without conftest paths and without unnamed plugins: the D06
                  inventory); nested_token and parent_pid (for a nested run and its workers, else null); run_id (the
                  `--experiment-record-run` id of the run, passed to its workers and to a nested run, else null).
collection-finish count (items collected), raw_ids_sha256 (SHA-256 over the raw ids joined by "\\n"),
                  digest_algorithm ("sha256").  Not written by an xdist controller.
collection-manifest  the ordered collected identities: source ("session" or "node"), source_worker (the worker id a
                  "node" manifest is about, else null), count, raw_ids (the raw node ids in collection order).  A
                  "session" manifest is written by every process that collects (serial, worker, nested) and also
                  carries identities, one per raw id in the same order: {canonical_id, param_id, requested_group,
                  effective_group}.  A "node" manifest is written by the xdist controller, one per worker, from
                  xdist's own `pytest_xdist_node_collection_finished`: the raw ids exactly as xdist received them
                  (a controller has no item objects, so it has no identities).  Written before any item runs, so a
                  run that is interrupted still shows every collected item, including the ones that never started.
item-start        nodeid (the raw node id, with the `@group` suffix xdist adds), canonical_id (the raw id minus the
                  suffix, removed only when it equals the requested group), param_id (the parametrize id of the item,
                  or null), requested_group (the xdist_group marker's name or null), effective_group (the group when
                  the raw id ends with `@<group>`, else null), suffix_missing (bool: a group was requested but the
                  raw id lacks its suffix).
phase             one per setup/call/teardown report: nodeid, canonical_id, param_id, requested_group and
                  effective_group (the same four identity fields as item-start, taken from the item while it was
                  available, so EVERY phase carries them), when ("setup", "call", "teardown", or "???" for the
                  controller's report of a crashed worker), outcome ("passed", "failed", "skipped"), duration,
                  reason (skip or xfail reason, bounded, else null), xfail (bool).  A failed phase also carries
                  the error fields below.  The controller writes only the "???" report, never the workers' phases.
                  The "???" report has no item object behind it, so its four identity fields are null and its
                  identity is the manifest row of the same raw id: the reducer joins them and shows the row as
                  `resolved` on that entry (for example in first_failure).
item-end          nodeid, elapsed (monotonic seconds since item-start).
collection-error  a failed collection report: nodeid and the error fields.
internal-error    `pytest_internalerror`: the error fields.
worker-down       controller only, xdist's `pytest_testnodedown`: down_worker (the worker id), has_error (bool); the
                  error fields when has_error.
nested-launch     written by `nested_environment`: token (16 hex digits) for the nested run it prepares.
exit-status       at session finish: exitstatus (int).  0 and 1 are not errors; any other value is.
trailer           last record: complete (true), records (how many records came before it, every kind counted),
                  exitstatus, plugins.

Error fields (on phase, collection-error, internal-error, worker-down)
-----------------------------------------------------------------------
error_text            the text AFTER cleaning (below), cut to at most 64 KiB (MAX_ERROR_TEXT) of UTF-8.
error_truncated       true when the cleaned text was cut.
error_length          the length in UTF-8 bytes of the ORIGINAL full text.
error_digest          SHA-256 over the UTF-8 bytes of the ORIGINAL `str(report.longrepr)` (not the cleaned or cut
                      one), lowercase hex.  A lone surrogate is encoded as a backslash escape first.
error_digest_algorithm  "sha256": the record names the algorithm.
error_redactions      how many replacements the cleaning made in this text.
A phase record's skip or xfail `reason` is cleaned the same way and carries `reason_redactions`.

Free text and what is blanked out
---------------------------------
Failure text, collection and internal error text, worker-down text and skip or xfail reasons can hold values a test
printed or compared.  Before such text is stored (and before it is cut), and after its digest and length were taken
from the original, the recorder replaces, longest first:
  * every value of the recording process's own environment that is 8 characters or longer, and every
    `os.pathsep`-separated part of such a value, by `<env:NAME>` (blank values are skipped), except the variables
    named `PYTEST_CURRENT_TEST`, `PYTEST_VERSION` or starting with `PYTEST_XDIST_` (below);
  * the home folder by `<home>`, the temp folder by `<temp>`, the record folder by `<record>` and the basetemp by
    `<basetemp>`, written with either slash and, on Windows, in any letter case.
Each is also found with its backslashes doubled, as a traceback shows a string from the source.  A shortened form is
joined back to its value too: pytest shows a long compared value as `head...tail`, and when `head` (at least 4
characters) ends where this text puts the `...` and starts a recognised value, and `tail` (at least 4) follows it and
ends the same value, the whole form becomes the same `<env:NAME>`, counted in the redactions.  Whole values and
shortened forms are both looked for in the ORIGINAL text, so a value that is the start of another cannot hide the
other's shortened form; spans that overlap become one replacement (the label of the longest), spans that only touch stay
two.  When several values fit one form, each is matched and the masked stretch is the union of their spans.  A lone
piece is masked as well: pytest shortens the repr of a list or dict, so a value first in it shows only its head before
the `...` and a value last shows only its tail after it; a stretch of at least 8 characters that ends at a `...` and
starts a recognised value, or starts right after a `...` and ends one, becomes that value's `<env:NAME>`.  The search is
bounded: each `...` looks only at the 256 characters around it, and only at places where a
recognised value begins, so a 64 KiB text full of `...` is cleaned in about a second at worst (tests allow 5).
LIMITS, stated plainly: only environment values and those four folders are recognised.
  * Values shorter than 8 characters cannot be recognised and are stored as they are.
  * A value shown in another spelling (escaped quotes or non-ASCII characters in a Python repr) is stored.
  * A value pytest shortens is recognised only by its first and last characters; a form with a piece under 4
    characters stays as it is, and so does a lone head or tail under 8 characters.
  * Node ids are kept exactly as pytest makes them (a value used in a parametrize id is stored), because the
    reducer joins on them.
  * Secrets that come from anywhere else (a file, a prompt, a network reply) cannot be recognised.
  * pytest's own run-time variables (`PYTEST_CURRENT_TEST`, `PYTEST_VERSION` and every `PYTEST_XDIST_*`) are never
    masked: they are not secrets, and the current test's id overlaps ordinary error text (`test_mod.py:6:
    AssertionError`), so masking it would corrupt the evidence.  Other `PYTEST_*` variables (such as
    `PYTEST_ADDOPTS`, which a person types) are masked like any other value.
  * Several environment values crafted on purpose to overlap or compete are defended against only in the cases the
    tests cover; this cleaning is a safety net, not a guarantee, which is why the journals are handled like test logs.
Handle journals like test logs.  The probe jobs run without secrets (plan section 4).

The reducer
-----------
`reduce_journals(folder, expected_ids=None, nested_seen=None, *, require_phases=True, run_id=None)` returns a dict:

schema            2.
status            "complete" when every journal ends with its trailer, else "incomplete".
accepted          true only when status is "complete", there is no problem and no error record.  A failing test
                  does not by itself stop acceptance; it is reported in first_failure.
problems          a list of {"kind", "detail", ...}; at most 20 per kind are listed, problem_counts has the totals.
                  Kinds: journal-not-plain (not a plain file with one name, a link or a reparse point, or changed
                  while it was opened), folder-not-plain (the record folder itself is a link or a reparse point),
                  folder-changed (the folder is not the one that was checked, or a journal lies outside it: the
                  journal is not used), folder-not-held (the record folder could not be opened and held, for
                  example on a share that refuses it: nothing is read, there is no fallback to path checks),
                  input-too-large (more journals or bytes than the limits below: none is parsed),
                  journal-too-large, bad-encoding (a complete line that is not UTF-8),
                  torn-last-line (a last line with no end: its writer died; every complete line before it is kept),
                  bad-line (not a JSON record), bad-field (a record with a damaged field: the record is set aside,
                  the rest is kept), sequence-gap, no-process-start, plugin-inventory-missing, no-trailer,
                  records-after-trailer, trailer-count-mismatch, item-unfinished (an item started and never ended;
                  it lists phases_seen and crash_recorded: whether a worker-down error for that worker or a "???"
                  report for that node id exists), crash-unrecorded (an unfinished item of a worker that no crash
                  record explains), suffix-missing, group-split, duplicate-id, missing-id, unexpected-id,
                  nested-journal-missing, nested-journal-unlaunched, and the evidence checks:
                  controller-ran-items (a root xdist controller journal holds an item-start: it runs no item),
                  manifest-missing (a serial or worker process wrote no collection manifest, and any process that
                  started items without one, a nested xdist controller included; also a nested serial
                  run or a nested worker, then with `nested_run`: absent evidence is never read as none),
                  controller-missing (worker journals of a run and no controller journal: its node manifests
                  and crash records cannot be there either; a nested xdist run's controller journal has the role
                  "nested", and the check is made per run token),
                  run-mismatch (only when `run_id` is given: a journal whose process-start has another run id, or none:
                  it is evidence of an earlier run in a reused folder, for example when the new invocation died
                  before `pytest_configure` and wrote nothing), folder-unreadable (the record folder exists but
                  cannot be examined, for example a permission error: only a missing folder is silent),
                  phase-identity-mismatch (a phase's canonical_id, param_id or group is not its running item's) and
                  item-identity-mismatch (an item-start whose identity is not what its node id and its own manifest
                  row give), bad-field also for an event kind outside the schema (EVENT_KINDS) and for a
                  process-start whose worker id or nested token does not fit its role (a nested process needs a
                  token; a worker needs its id; a serial or controller process has neither),
                  journal-too-many-records (more lines than MAX_JOURNAL_RECORDS in one journal, or than
                  MAX_TOTAL_RECORDS across the journals: counted before the journal is split, so a file of newline
                  bytes costs nothing; none of it is read), manifest-identity-mismatch (a session manifest pairs a raw
                  id with an identity that does not follow from it: the canonical id is the raw id minus its group
                  suffix, the effective group is the requested group exactly when the suffix is there),
                  item-not-in-manifest (an item started that its own process's session manifest never listed,
                  wherever the start and the manifest stand in the journal: both are known before it is judged),
                  manifest-duplicate-id (a manifest lists a raw id twice, or two raw ids with one canonical id),
                  item-end-outside-item (an item-end with no running item: it is not counted either),
                  exit-status-mismatch (the exit-status event and the completion trailer disagree; the trailer
                  carries the exit status, required, and a trailer status other than 0 or 1 is an error on its own,
                  so first_error can be the trailer),
                  multiple-root-runs (more than one root serial journal, more than one root controller, or one
                  worker id twice: stale evidence from an earlier run in a reused folder cannot be told from this
                  run's) and mixed-root-topology (a root serial run and a root xdist run in one folder),
                  multiple-nested-runs (the same rule per nested token: two `nested` journals, or one worker id
                  twice, under one token; it carries `nested_run`),
                  workers-collected-differently (the workers of one run did not all collect the same ordered list,
                  raw ids and identities: xdist gives them one common collection; per run, with `nested_run`),
                  crash-report-outside-controller (a "???" crash report in a serial or worker journal: only a
                  controller writes one, and only a controller's report excuses an unfinished item),
                  crash-report-unknown-item (a controller's "???" report for an id none of its node manifests lists),
                  worker-evidence-missing (a root controller journal and neither a worker journal nor a node manifest),
                  nested-run-unreachable (a nested run whose launch record is not in a reachable run, or not from the
                  process its journals name as their `parent_pid`: reachable means launched, through such records, from
                  a root process), duplicate-id also per nested run (an item started twice in one nested run, with
                  `nested_run`), suffix-missing derived from the node id and the requested group (a persisted
                  `suffix_missing` that disagrees is an item-identity-mismatch; this is checked for every process that
                  runs items, a nested run's too, by the writer's rule: an empty group name is a group),
                  folder-unreadable also when the held
                  folder cannot be listed,
                  manifest-disagreement (the controller's manifest for a worker is missing or differs from the
                  worker's own), collection-mismatch (the collected canonical ids differ from `expected_ids`),
                  never-started (collected but never started: the run stopped before it; checked for every
                  nested run on its own), phase-missing (an item that ended lacks its setup, its call after a
                  setup that passed, or its teardown: lost evidence or an interrupted teardown; a phase's `when`
                  and `outcome` must be words pytest uses, else bad-field), phase-order (the phases are not
                  setup, call, teardown once each in that
                  order, or a call came after a setup that did not pass) and phase-outside-item (a phase report
                  with no started item), phase-in-no-body-probe (under `require_phases=False`, a started item
                  reported a phase).  Each item gets at most one phase problem.  A problem of a nested run
                  carries `nested_run` (its token).
first_error       the earliest error record (collection-error, internal-error, worker-down with an error,
                  exit-status other than 0 or 1) across all journals, ordered by t_wall, journal name, seq; or
                  null.  Nothing written later, a trailer included, replaces it.
first_failure     the earliest failed phase, the same way, or null.
processes         per journal: file, role, worker, pid, complete, records, plugins, exitstatus, nested_token.
items             canonical id -> {started, ended, workers, group}, from serial/worker journals (nested runs are
                  separate).  The canonical id is the raw id minus a trailing `@<requested group>`, removed only
                  when it equals that suffix (`canonical_id`).
groups            effective group -> sorted worker ids that started its members.
collection        {"collected": how many canonical ids the manifests list (null when there is none),
                  "never_started": those that no journal started, in collection order,
                  "nodes": {worker id: how many ids the controller recorded for it}}.
nested            {"launched": launches written, "journaled": launches that have a nested journal}.

When `expected_ids` (the serial collection's ids) is given, the join must be one to one: each expected id started
exactly once, and the manifests must list the same canonical ids.  A serial run is a one-worker run.
The phases of every ended item are reconciled with what pytest does: a missing outcome is a problem, so a run
whose phase writer was off, or whose phase lines were removed, is not accepted.  Nested journals are reconciled
the same way, run by run (their ids stay out of the root run's collection and join).  A probe run that
deliberately runs no test bodies (items start and end with no phase) passes `require_phases=False`: that drops the
missing-phase result and instead requires that a started item has NO phase at all (any phase is
phase-in-no-body-probe, which also catches a probe that ran a body by mistake and phases out of order), and
keeps every other check.
Size: a journal takes about 3 KB per item with its identity and the manifest (a 12,000-item serial run is about
36 MiB); a run with many failing tests (each error text up to 64 KiB) can pass MAX_JOURNAL_BYTES, and the reducer
then refuses that journal as journal-too-large instead of reading it.  P8's dry run measures the real serial
journal size, which settles the margin.

Nested pytest runs
------------------
Stale journals.  A record folder may be reused, and a new invocation can die before it writes anything (an early
conftest or option failure), leaving the previous run's complete journal as the only one.  An empty-folder rule at
launch cannot help, because the launch never gets that far; so the caller gives the run an id
(`--experiment-record-run=ID`, 1 to 64 letters, digits, dots, dashes or underscores) and passes the same id to
`reduce_journals(..., run_id=ID)`, which refuses (`run-mismatch`) every journal of another run.  The id is copied to
the workers (xdist `workerinput`) and to a nested run (the handoff).  Without `run_id` the reducer cannot tell.

Recording through PYTEST_ADDOPTS.  `--experiment-record` and `--experiment-record-run` may be given in PYTEST_ADDOPTS.
Once the recorder is registered it removes both from this process's PYTEST_ADDOPTS (the other options stay; the
variable is deleted if nothing else was in it), so an ordinary pytest child, which inherits the environment, does not
record: only an explicit handoff makes a nested run.  Limit: an option given through an `@file` or an ini `addopts`
cannot be removed from there.

A nested run is recorded only through an explicit handoff that the recorder alone sets, for the one process it
starts: `nested_environment(env)` returns a copy of `env` with `HANDOFF_ENV` (a JSON object with v, dir, token,
parent_pid) and writes a `nested-launch` record.  The recorder never puts the variable into its own environment, and
a nested run removes it from its own, so ordinary nested runs do not inherit it.  Without an active recorder the
helper hands off nothing.  A launch with no nested journal is a failure (`nested-journal-missing`), never
compliance; so is `nested_seen` larger than the number of nested journals.  Workers get the folder through
xdist's `workerinput`, never through the environment.  A valid nested handoff decides the role: a child that also
inherits `--experiment-record` (for example through PYTEST_ADDOPTS) is still a nested run, never a top-level one.

Limits of the reducer
---------------------
It reads the journals of one plain record folder (a link or a reparse point is refused, the folder and each
journal alike, and a journal must still be the same file after it is opened): at most 512 journals and 256 MiB
of journal bytes together, checked before any is parsed (MAX_JOURNALS, MAX_TOTAL_JOURNAL_BYTES), and 64 MiB for
one journal; and at most 500,000 lines in one journal and 1,000,000 together (MAX_JOURNAL_RECORDS,
MAX_TOTAL_RECORDS), counted with the bytes in hand and before the split.  More is refused with
input-too-large or journal-too-many-records, not read; parsed records take several times their bytes in memory,
which is why the totals are bounded.

The folder protection.  The record folder is opened ONCE and that held object is the baseline: its device, file
id and mode, and on Windows its own final path (GetFinalPathNameByHandleW of the handle, never a second lookup
of the path), so its identity is kept for every journal open.  On Windows the handle is opened as the reparse
point itself (a junction or link is refused) and without delete sharing, so the operating system refuses to
rename or replace the folder while it is held; each journal's final path must be its name inside the held
folder's final path.  On POSIX the journals are listed and opened relative to the folder's own descriptor
(`O_NOFOLLOW`), so no path is resolved again.  Everywhere, after each open the folder path must still have the
same device, file id and mode and not be a link or a reparse point.  Any mismatch is refused as folder-changed
and nothing from that journal is used; a folder that cannot be opened and held is folder-not-held, never
a silent fallback to path checks.  What cannot be closed: a folder that was already a different real directory
when it was first opened (there is no earlier baseline), a journal changed while it is read, a filesystem that
ignores sharing modes (the final-path and path rechecks then remain), and, on Windows, the list of names,
which comes from the path (a name that is not in the held folder is refused when opened).  Whoever can do
that can write the evidence anyway.
"""

from __future__ import annotations

import bisect
import contextvars
import fnmatch
import functools
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

SCHEMA_VERSION = 2
HANDOFF_VERSION = 1
OPTION = "--experiment-record"
RUN_OPTION = "--experiment-record-run"
PLUGIN_NAME = "agenttalk-experiment-record"
HANDOFF_ENV = "AGENTTALK_EXPERIMENT_RECORD_HANDOFF"
MAX_ERROR_TEXT = 64 * 1024
MAX_REASON_TEXT = 4096
MAX_HANDOFF_BYTES = 4096
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_TOTAL_JOURNAL_BYTES = 256 * 1024 * 1024
MAX_JOURNALS = 512
MAX_JOURNAL_RECORDS = 500_000
MAX_TOTAL_RECORDS = 1_000_000
MAX_PROBLEMS_PER_KIND = 20
DIGEST_ALGORITHM = "sha256"
MIN_SECRET_LENGTH = 8
# pytest's and xdist's run-time variables, which are set by the tools and are not secrets; a name like PYTEST_ADDOPTS
# is typed by a person and can carry a value, so it is masked like any other.
RUN_VARIABLES = frozenset({"PYTEST_CURRENT_TEST", "PYTEST_VERSION"})
RUN_VARIABLE_PREFIXES = ("PYTEST_XDIST_",)
MIN_SHORTENED_PIECE = 4
SHORTENED_WINDOW = 256
ROLE_NAMES = ("serial", "controller", "worker", "nested")
PHASE_NAMES = ("setup", "call", "teardown", "???")
PHASE_OUTCOMES = ("passed", "failed", "skipped")

_TOKEN = re.compile(r"[0-9a-f]{16}")
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Every event kind the schema writes; a record of any other kind is a damaged record, never skipped.
EVENT_KINDS = (
    "process-start",
    "collection-finish",
    "collection-manifest",
    "item-start",
    "phase",
    "item-end",
    "collection-error",
    "internal-error",
    "worker-down",
    "nested-launch",
    "exit-status",
    "trailer",
)
IDENTITY_FIELDS = ("canonical_id", "param_id", "requested_group", "effective_group")
_ACTIVE: Recorder | None = None


def add_option(parser: pytest.Parser) -> None:
    parser.addoption(
        OPTION,
        dest="experiment_record",
        default=None,
        metavar="DIR",
        help="record what this run did in durable per-process journals in DIR (outside every basetemp)",
    )
    parser.addoption(
        RUN_OPTION,
        dest="experiment_record_run",
        default=None,
        metavar="ID",
        help="the id of this run (1 to 64 letters, digits, dots, dashes, underscores), written into every journal so "
        "that the reducer can tell this run's journals from an earlier run's in a reused folder",
    )


def canonical_id(raw_id: str, requested_group: str | None) -> str:
    """The raw id minus xdist's `@<group>` suffix, removed only when it equals the requested group (empty name too)."""
    if requested_group is not None:
        suffix = "@" + requested_group
        if raw_id.endswith(suffix):
            return raw_id[: -len(suffix)]
    return raw_id


def _suffix_missing(raw_id: str, requested_group: str | None) -> bool:
    """The writer's rule exactly: a group was requested (an empty name counts) and the node id does not end with it."""
    return requested_group is not None and not raw_id.endswith("@" + requested_group)


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "backslashreplace")


def _bounded(text: str, limit: int) -> tuple[str, bool, str, int]:
    """(kept text, truncated, SHA-256 hex of the full text, full length in bytes)."""
    data = _encode(text)
    kept = data[:limit].decode("utf-8", "ignore") if len(data) > limit else data.decode("utf-8")
    return kept, len(data) > limit, hashlib.sha256(data).hexdigest(), len(data)


@functools.lru_cache(maxsize=8)
def _pattern(needles: tuple[str, ...], ignorecase: bool) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(n) for n in needles), re.IGNORECASE if ignorecase else 0)


def _same_text(value: str) -> str:
    return value


def _folder(ignorecase: bool):
    return str.casefold if ignorecase else _same_text


@functools.lru_cache(maxsize=8)
def _tail_index(ordered: tuple[str, ...], ignorecase: bool) -> dict[str, list[tuple[str, int]]]:
    """{the first MIN_SECRET_LENGTH characters of a possible lone tail: [(value, where that tail starts in it)]}, for
    tails of at most SHORTENED_WINDOW characters; for each value the longest tail comes first."""
    fold = _folder(ignorecase)
    index: dict[str, list[tuple[str, int]]] = {}
    for needle in ordered:
        for where in range(max(0, len(needle) - SHORTENED_WINDOW), len(needle) - MIN_SECRET_LENGTH + 1):
            index.setdefault(fold(needle[where : where + MIN_SECRET_LENGTH]), []).append((needle, where))
    return index


@functools.lru_cache(maxsize=8)
def _heads_pattern(heads: tuple[str, ...], ignorecase: bool) -> re.Pattern[str]:
    """Finds, at every position (overlapping), the first MIN_SHORTENED_PIECE characters of some value."""
    return re.compile("(?=(" + "|".join(re.escape(h) for h in heads) + "))", re.IGNORECASE if ignorecase else 0)


def _shortened_spans(
    text: str, ordered: tuple[str, ...], found: dict[str, str], ignorecase: bool
) -> list[tuple[int, int, str]]:
    """(start, end, label) of each `head...tail` in `text` that is one recognised value shown shortened, and of each
    lone head or lone tail of one.

    pytest cuts a long compared value to its first and last characters with "..." between them, so neither piece is
    the whole value. Both pieces must be at least MIN_SHORTENED_PIECE characters, `head` must be a prefix and `tail`
    a suffix of the same value, each within SHORTENED_WINDOW characters of the "...". A head can only start where
    some value begins, so only those places are tried for each "...". When pytest cuts the repr of a list or dict
    instead, only one piece of the value shows: a stretch of at least MIN_SECRET_LENGTH characters ending at a "..."
    that starts a value, or starting right after one that ends a value, is a span of its own."""
    if "..." not in text:
        return []
    fold = _folder(ignorecase)
    by_head: dict[str, list[str]] = {}
    for needle in ordered:
        if len(needle) >= 2 * MIN_SHORTENED_PIECE:
            by_head.setdefault(fold(needle[:MIN_SHORTENED_PIECE]), []).append(needle)
    if not by_head:
        return []
    heads = tuple({needle[:MIN_SHORTENED_PIECE] for group in by_head.values() for needle in group})
    starts = [(match.start(), fold(match.group(1))) for match in _heads_pattern(heads, ignorecase).finditer(text)]
    positions = [start for start, _ in starts]
    tails = _tail_index(ordered, ignorecase)
    spans: list[tuple[int, int, str]] = []
    position = text.find("...")
    while position != -1:
        first = bisect.bisect_left(positions, position - SHORTENED_WINDOW)
        last = bisect.bisect_right(positions, position - MIN_SHORTENED_PIECE)
        spans.extend(_shortened_spans_at(text, position, starts[first:last], by_head, found, fold))
        spans.extend(_lone_tail_spans(text, position + 3, tails, found, fold))
        position = text.find("...", position + 3)
    return spans


def _shortened_spans_at(text, position, candidates, by_head, found, fold):
    """The spans for the "..." at `position`: for every recognised value that fits, one from its longest head and
    longest tail. Several values can fit one form and show different lengths of the tail, so none is left out; the
    spans are merged afterwards. The longest head of a value that is MIN_SECRET_LENGTH characters or more is a lone
    head and a span of its own."""
    spans = []
    whole = set()
    lone_starts = set()
    right = text[position + 3 : position + 3 + SHORTENED_WINDOW]
    for start, key in candidates:
        head = text[start:position]
        for needle in by_head[key]:
            if len(head) > len(needle) or fold(head) != fold(needle[: len(head)]):
                continue
            if len(head) >= MIN_SECRET_LENGTH and start not in lone_starts:
                lone_starts.add(start)
                spans.append((start, position, found[needle]))
            if needle not in whole and len(head) <= len(needle) - MIN_SHORTENED_PIECE:
                whole.add(needle)  # candidates run from the longest head, so this is the longest head of this value
                tail = _longest_tail(right, needle, fold)
                if tail:
                    spans.append((start, position + 3 + tail, found[needle]))
    return spans


def _lone_tail_spans(text, start, index, found, fold):
    """A span for every recognised value whose last MIN_SECRET_LENGTH or more characters are the text from `start`."""
    spans = []
    seen = set()
    for needle, where in index.get(fold(text[start : start + MIN_SECRET_LENGTH]), ()):
        size = len(needle) - where
        if needle not in seen and fold(text[start : start + size]) == fold(needle[where:]):
            seen.add(needle)  # the longest tail of this value comes first
            spans.append((start, start + size, found[needle]))
    return spans


def _longest_tail(side: str, needle: str, fold) -> int:
    """The longest length k (at least MIN_SHORTENED_PIECE, leaving as much for the head) such that the first k
    characters of `side` are the last k of `needle`; 0 when none."""
    limit = min(len(side), len(needle) - MIN_SHORTENED_PIECE)
    if limit < MIN_SHORTENED_PIECE:
        return 0
    folded_side, folded_needle = fold(side[:limit]), fold(needle)
    if len(folded_side) != limit or len(folded_needle) != len(needle):
        # Folding changed a length (a letter like the German sharp s): compare piece by piece, the slow way.
        for size in range(limit, MIN_SHORTENED_PIECE - 1, -1):
            if fold(side[:size]) == fold(needle[-size:]):
                return size
        return 0
    index = folded_needle.find(folded_side[0], len(needle) - limit)  # only where the tail's first character occurs
    while index != -1 and len(needle) - index >= MIN_SHORTENED_PIECE:
        if folded_needle.endswith(folded_side[: len(needle) - index]):
            return len(needle) - index
        index = folded_needle.find(folded_side[0], index + 1)
    return 0


def _replace_spans(text: str, spans: list[tuple[int, int, str]]) -> tuple[str, int]:
    """(`text` with each span replaced by its label, the number of replacements). Spans that overlap are one
    replacement, with the label of the longest of them; spans that only touch stay separate."""
    merged: list[list[Any]] = []  # [start, end, label, length of the longest span so far]
    for start, end, label in sorted(spans, key=lambda span: (span[0], -span[1])):
        if merged and start < merged[-1][1]:
            last = merged[-1]
            last[1] = max(last[1], end)
            if end - start > last[3]:
                last[2], last[3] = label, end - start
        else:
            merged.append([start, end, label, end - start])
    pieces: list[str] = []
    cursor = 0
    for start, end, label, _ in merged:
        pieces.extend((text[cursor:start], label))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), len(merged)


class Redactor:
    """Blanks out what a journal must not show (see "Free text and what is blanked out" in the module docstring).

    The needles are read from the environment and the folders at every call, so a value set after the recorder
    started is still recognised."""

    def __init__(self, config: pytest.Config, directory: Path) -> None:
        self.config = config
        self.directory = directory

    def _basetemp(self) -> str:
        factory = getattr(self.config, "_tmp_path_factory", None)
        if factory is not None:
            return str(factory.getbasetemp())
        given = self.config.option.basetemp
        return os.path.abspath(given) if given else ""

    def needles(self) -> dict[str, str]:
        """{text to find: replacement}."""
        found: dict[str, str] = {}
        for name, value in os.environ.items():
            if name.upper() in RUN_VARIABLES or name.upper().startswith(RUN_VARIABLE_PREFIXES):
                continue  # pytest's and xdist's own run variables: not secrets, and masking them corrupts evidence
            for part in {value, *value.split(os.pathsep)}:
                if len(part) >= MIN_SECRET_LENGTH and part.strip():
                    found.setdefault(part, f"<env:{name}>")
        folders = {
            "<home>": os.path.expanduser("~"),
            "<temp>": tempfile.gettempdir(),
            "<record>": str(self.directory),
            "<basetemp>": self._basetemp(),
        }
        for label, folder in folders.items():
            if len(folder) >= 3 and folder.strip("\\/"):
                for form in {folder, folder.replace("\\", "/"), folder.replace("/", "\\")}:
                    found[form] = label
        # A traceback shows a string from the source line as Python writes it, with every backslash doubled.
        for needle, label in list(found.items()):
            if "\\" in needle:
                found.setdefault(needle.replace("\\", "\\\\"), label)
        return found

    def clean(self, text: str) -> tuple[str, int]:
        """(the text with every needle replaced, longest first; the number of replacements). Whole values and
        shortened forms are both found in the original text, then replaced together."""
        found = self.needles()
        if not found:
            return text, 0
        ignorecase = os.name == "nt"
        ordered = tuple(sorted(found, key=lambda needle: (-len(needle), needle)))
        labels = {(n.casefold() if ignorecase else n): found[n] for n in ordered}

        def label(match: re.Match[str]) -> str:
            key = match.group(0)
            return labels.get(key.casefold() if ignorecase else key, "<redacted>")

        spans = [(match.start(), match.end(), label(match)) for match in _pattern(ordered, ignorecase).finditer(text)]
        spans.extend(_shortened_spans(text, ordered, found, ignorecase))
        return _replace_spans(text, spans)


_NO_IDENTITY = {"canonical_id": None, "param_id": None, "requested_group": None, "effective_group": None}


def _requested_group(item: pytest.Item) -> str | None:
    """The group name xdist uses for this item (the same rule as xdist's own collection hook)."""
    mark = item.get_closest_marker("xdist_group")
    if mark is None:
        return None
    return str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))


def open_journal(directory: Path, role: str, pid: int, started_ms: int) -> tuple[Path, int]:
    """Create the journal exclusively and return (path, file descriptor); an existing name is never reused."""
    path = Path(directory) / f"journal-{role}-{pid}-{started_ms}.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"{path} is not a plain file with a single name")
    except BaseException:
        os.close(fd)
        raise
    return path, fd


class Recorder:
    """The plugin: one journal for this process, one line per event."""

    def __init__(
        self,
        config: pytest.Config,
        directory: Path,
        role: str,
        worker: str | None,
        token: str | None,
        parent_pid: int | None,
        distributed: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.config = config
        self.distributed = distributed  # an xdist controller: the workers' journals hold the phases
        self.directory = directory
        self.role = role
        self.worker = worker
        self.token = token
        self.parent_pid = parent_pid
        self.run_id = run_id
        self.pid = os.getpid()
        self.started_ms = int(time.time() * 1000)
        self.seq = 0
        self.path, self._fd = open_journal(directory, role, self.pid, self.started_ms)
        self._redactor = Redactor(config, directory)
        self._identities: dict[str, dict[str, Any]] = {}  # raw node id -> the item's identity, kept for its phases
        self._closed = False

    # -- writing ----------------------------------------------------------------------------------------------

    def append(self, kind: str, **fields: Any) -> None:
        self.seq += 1
        record = {
            "v": SCHEMA_VERSION,
            "seq": self.seq,
            "kind": kind,
            "role": self.role,
            "worker": self.worker,
            "pid": self.pid,
            "t_wall": time.time(),
            "t_mono": time.monotonic(),
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        view = memoryview(_encode(line))
        while view:
            view = view[os.write(self._fd, view) :]
        os.fsync(self._fd)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)

    # -- free text --------------------------------------------------------------------------------------------

    def error_fields(self, text: str) -> dict[str, Any]:
        """The error fields of `text`: digest and length from the ORIGINAL text, then the stored copy is cleaned
        and only then cut to the limit (so a value that straddles the cut cannot leave a fragment)."""
        original = _encode(text)
        cleaned, redactions = self._redactor.clean(text)
        kept, truncated, _digest, _length = _bounded(cleaned, MAX_ERROR_TEXT)
        return {
            "error_text": kept,
            "error_truncated": truncated,
            "error_length": len(original),
            "error_digest": hashlib.sha256(original).hexdigest(),
            "error_digest_algorithm": DIGEST_ALGORITHM,
            "error_redactions": redactions,
        }

    def reason_fields(self, reason: Any) -> dict[str, Any]:
        """A skip or xfail reason, cleaned and cut."""
        if reason is None:
            return {"reason": None, "reason_redactions": 0}
        cleaned, redactions = self._redactor.clean(str(reason))
        return {"reason": _bounded(cleaned, MAX_REASON_TEXT)[0], "reason_redactions": redactions}

    # -- start ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        option = self.config.option
        self.append(
            "process-start",
            started_ms=self.started_ms,
            python=sys.version.split()[0],
            pytest=pytest.__version__,
            dist=getattr(option, "dist", None),
            loadgroup=bool(getattr(option, "loadgroup", False)),
            plugins=self._plugins(),
            nested_token=self.token,
            parent_pid=self.parent_pid,
            run_id=self.run_id,
        )

    def _plugins(self) -> list[str]:
        names = (name for name, plugin in self.config.pluginmanager.list_name_plugin() if plugin is not None)
        return sorted(n for n in names if not n.isdigit() and not any(c in n for c in "/\\:"))

    # -- collection -------------------------------------------------------------------------------------------

    def _identity(self, item: pytest.Item) -> dict[str, Any]:
        """Who the item is, taken while the item object is available: the canonical id, the parameter id and the
        group it asked for and the group its node id shows."""
        raw = item.nodeid
        group = _requested_group(item)
        suffixed = group is not None and raw.endswith("@" + group)
        param = getattr(getattr(item, "callspec", None), "id", None)
        return {
            "canonical_id": canonical_id(raw, group),
            "param_id": None if param is None else str(param),
            "requested_group": group,
            "effective_group": group if suffixed else None,
        }

    @pytest.hookimpl(trylast=True)  # after conftest.py's workaround that copies xdist's group suffix
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        ids = [item.nodeid for item in session.items]
        identities = [self._identity(item) for item in session.items]
        self._identities.update(zip(ids, identities, strict=True))
        self.append(
            "collection-finish",
            count=len(ids),
            digest_algorithm=DIGEST_ALGORITHM,
            raw_ids_sha256=hashlib.sha256(_encode("\n".join(ids))).hexdigest(),
        )
        self.append(
            "collection-manifest",
            source="session",
            source_worker=None,
            count=len(ids),
            raw_ids=ids,
            identities=identities,
        )

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: Sequence[str]) -> None:
        """The controller's manifest of what one worker collected, as xdist received it (raw ids, in order)."""
        self.append(
            "collection-manifest", source="node", source_worker=node.gateway.id, count=len(ids), raw_ids=list(ids)
        )

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.append("collection-error", nodeid=report.nodeid, **self.error_fields(str(report.longrepr)))

    # -- running ----------------------------------------------------------------------------------------------

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None):
        raw = item.nodeid
        identity = self._identities[raw] = self._identity(item)
        started = time.monotonic()
        self.append(
            "item-start",
            nodeid=raw,
            canonical_id=identity["canonical_id"],
            param_id=identity["param_id"],
            requested_group=identity["requested_group"],
            effective_group=identity["effective_group"],
            suffix_missing=identity["requested_group"] is not None and identity["effective_group"] is None,
        )
        yield  # the outcome is never read, so an exception from another hook propagates unchanged
        self.append("item-end", nodeid=raw, elapsed=time.monotonic() - started)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if self.distributed and report.when != "???":
            return  # the workers' own journals hold their phases
        extra: dict[str, Any] = {}
        reason = getattr(report, "wasxfail", None)
        if reason is None and report.skipped and isinstance(report.longrepr, tuple):
            reason = report.longrepr[2]
        if report.failed:
            extra.update(self.error_fields(str(report.longrepr)))
        identity = self._identities.get(report.nodeid) or _NO_IDENTITY  # the controller has no item for a crash
        self.append(
            "phase",
            nodeid=report.nodeid,
            **identity,
            when=report.when,
            outcome=report.outcome,
            duration=report.duration,
            xfail=hasattr(report, "wasxfail"),
            **self.reason_fields(reason),
            **extra,
        )

    # -- errors -----------------------------------------------------------------------------------------------

    # `tryfirst` is a precaution only: pytest 9.1.1 calls every implementation of this hook, so on that version the
    # order changes nothing (removing it is an equivalent mutant there).
    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(self, excrepr: Any, excinfo: Any = None) -> None:
        self.append("internal-error", **self.error_fields(str(excrepr)))

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        fields = self.error_fields(str(error)) if error else {}
        self.append("worker-down", down_worker=node.gateway.id, has_error=bool(error), **fields)

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: Any) -> None:
        node.workerinput["experiment_record_dir"] = str(self.directory)
        node.workerinput["experiment_record_token"] = self.token
        node.workerinput["experiment_record_parent_pid"] = self.parent_pid
        node.workerinput["experiment_record_run"] = self.run_id

    # -- finish -----------------------------------------------------------------------------------------------

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.append("exit-status", exitstatus=int(exitstatus))
        self.append("trailer", complete=True, records=self.seq, exitstatus=int(exitstatus), plugins=self._plugins())

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        global _ACTIVE
        if _ACTIVE is self:
            _ACTIVE = None
        self.close()

    # -- nested runs ------------------------------------------------------------------------------------------

    def handoff(self, env: dict[str, str]) -> dict[str, str]:
        token = secrets.token_hex(8)
        self.append("nested-launch", token=token)
        env[HANDOFF_ENV] = json.dumps(
            {
                "v": HANDOFF_VERSION,
                "dir": str(self.directory),
                "token": token,
                "parent_pid": self.pid,
                "run_id": self.run_id,
            },
            sort_keys=True,
        )
        return env


def nested_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `env` (default: the environment) for ONE nested pytest process the caller starts.

    With an active recorder it carries the handoff and a `nested-launch` record is written; otherwise any stale
    handoff is dropped and nothing is handed off.  Names are compared without regard to case (Windows)."""
    result = dict(os.environ if env is None else env)
    for name in [n for n in result if n.upper() == HANDOFF_ENV]:
        del result[name]
    return _ACTIVE.handoff(result) if _ACTIVE is not None else result


# -- wiring ---------------------------------------------------------------------------------------------------


def _basetemps(config: pytest.Config) -> set[Path]:
    found: set[Path] = set()
    given = config.option.basetemp
    if given:
        found.add(Path(os.path.abspath(given)).resolve())
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        basetemp = Path(factory.getbasetemp()).resolve()
        found.add(basetemp)
        if not given:
            found.add(basetemp.parent)  # pytest-of-<user>: older numbered folders there are deleted too
    return found


def _checked_directory(config: pytest.Config, text: str, *, create: bool, source: str) -> Path:
    path = Path(text)
    if not path.is_absolute():
        path = Path(config.invocation_params.dir) / path
    resolved = path.resolve()
    for basetemp in _basetemps(config):
        if resolved == basetemp or basetemp in resolved.parents:
            raise pytest.UsageError(
                f"{source}: the record folder {resolved} is inside the basetemp {basetemp}, where pytest would "
                f"delete it; give a folder outside every basetemp"
            )
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise pytest.UsageError(f"{source}: the record folder {resolved} is not an existing folder")
    return resolved


def _read_handoff(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8", "replace")) > MAX_HANDOFF_BYTES:
        raise pytest.UsageError(f"{HANDOFF_ENV} is too long")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise pytest.UsageError(f"{HANDOFF_ENV} is not valid JSON") from None
    if not isinstance(data, dict) or data.get("v") != HANDOFF_VERSION:
        raise pytest.UsageError(f"{HANDOFF_ENV} is not a version {HANDOFF_VERSION} handoff object")
    token, directory, parent = data.get("token"), data.get("dir"), data.get("parent_pid")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise pytest.UsageError(f"{HANDOFF_ENV} has a bad token")
    if not isinstance(directory, str) or not os.path.isabs(directory):
        raise pytest.UsageError(f"{HANDOFF_ENV} needs an absolute folder")
    if not isinstance(parent, int) or isinstance(parent, bool):
        raise pytest.UsageError(f"{HANDOFF_ENV} has a bad parent_pid")
    data["run_id"] = _checked_run_id(data.get("run_id"), HANDOFF_ENV)
    return data


def _checked_run_id(value: Any, source: str) -> str | None:
    """The run id, or None when there is none; a value that is not 1 to 64 safe characters is a usage error."""
    if value is None:
        return None
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise pytest.UsageError(f"{source}: the run id must be 1 to 64 letters, digits, dots, dashes or underscores")
    return value


def _without_record_options(text: str) -> str:
    """PYTEST_ADDOPTS text without the recording options (`--experiment-record` and `--experiment-record-run`, with
    or without `=`), so that a process started with the same environment is an ordinary one.  Other options are
    kept; text that cannot be split, or that holds neither option, is returned exactly as it was."""
    try:
        words = shlex.split(text)
    except ValueError:
        return text
    kept: list[str] = []
    skip_value = False
    for word in words:
        if skip_value:
            skip_value = False
        elif word in (OPTION, RUN_OPTION):
            skip_value = True
        elif not word.startswith((OPTION + "=", RUN_OPTION + "=")):
            kept.append(word)
    return text if kept == words else shlex.join(kept)


def _scrub_inherited_options() -> None:
    """Take the recording options out of this process's PYTEST_ADDOPTS: it is how a person turns recording on for the
    root run, and every ordinary pytest child would otherwise inherit it and write a second root.  A nested run is
    recorded only through the explicit handoff."""
    for name in [n for n in os.environ if n.upper() == "PYTEST_ADDOPTS"]:
        cleaned = _without_record_options(os.environ[name])
        if cleaned != os.environ[name]:
            if cleaned:
                os.environ[name] = cleaned
            else:
                del os.environ[name]


def configure(config: pytest.Config) -> Recorder | None:
    """Register the recorder if this process was asked to record; otherwise do nothing at all."""
    global _ACTIVE
    existing = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if existing is not None:
        return existing
    workerinput = getattr(config, "workerinput", None)
    handoff_text = None
    for name in [n for n in os.environ if n.upper() == HANDOFF_ENV]:
        handoff_text = os.environ.pop(name)  # a recorded process never passes the handoff on by accident
    token = parent = run_id = None
    if workerinput is not None:
        if not workerinput.get("experiment_record_dir"):
            return None
        directory = _checked_directory(
            config, workerinput["experiment_record_dir"], create=False, source="xdist workerinput"
        )
        role, worker, distributed = "worker", str(workerinput["workerid"]), False
        token, parent = workerinput.get("experiment_record_token"), workerinput.get("experiment_record_parent_pid")
        run_id = _checked_run_id(workerinput.get("experiment_record_run"), "xdist workerinput")
    elif handoff_text is not None:  # a valid handoff decides the role, whatever option was inherited with it
        data = _read_handoff(handoff_text)
        directory = _checked_directory(config, data["dir"], create=False, source=HANDOFF_ENV)
        role, worker, token, parent, run_id = "nested", None, data["token"], data["parent_pid"], data["run_id"]
        distributed = bool(getattr(config.option, "numprocesses", None))
    elif config.option.experiment_record:
        directory = _checked_directory(config, config.option.experiment_record, create=True, source=OPTION)
        run_id = _checked_run_id(config.option.experiment_record_run, RUN_OPTION)
        numprocesses = getattr(config.option, "numprocesses", None)
        role, worker, distributed = ("controller" if numprocesses else "serial"), None, bool(numprocesses)
    elif config.option.experiment_record_run:
        raise pytest.UsageError(f"{RUN_OPTION} needs {OPTION}")
    else:
        return None
    recorder = Recorder(config, directory, role, worker, token, parent, distributed, run_id)
    try:
        config.pluginmanager.register(recorder, PLUGIN_NAME)
        recorder.start()
    except BaseException:
        recorder.close()
        raise
    _ACTIVE = recorder
    _scrub_inherited_options()  # a child of this process is ordinary unless it is handed off explicitly
    return recorder


# -- the reducer ----------------------------------------------------------------------------------------------


class _Problems:
    def __init__(self) -> None:
        self.listed: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()

    def add(self, kind: str, detail: str, **extra: Any) -> None:
        self.counts[kind] += 1
        if self.counts[kind] <= MAX_PROBLEMS_PER_KIND:
            self.listed.append({"kind": kind, "detail": detail, **extra})


# The record folder being reduced, verified once by `reduce_journals` and held while its journals are opened.
_FOLDER: contextvars.ContextVar[Any] = contextvars.ContextVar("experiment_record_folder", default=None)
# The records the journals of one reduction may still hold together (MAX_TOTAL_RECORDS counts down as they are read).
_RECORDS_LEFT: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("experiment_record_left", default=None)
_HAS_DIR_FD = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd


def _identity(info: Any) -> tuple[Any, ...]:
    return tuple(getattr(info, key, None) for key in ("st_dev", "st_ino", "st_mode"))


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    return kernel


def _final_path(fd: int) -> str:
    """The real path of the file an open descriptor refers to, as Windows reports it for the handle
    (GetFinalPathNameByHandleW: every link and junction on the way resolved), without the long-path prefix."""
    import ctypes
    import msvcrt

    get = _kernel32().GetFinalPathNameByHandleW
    handle = msvcrt.get_osfhandle(fd)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = get(handle, buffer, size, 0)
        if length == 0:
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        if length < size:
            break
        size = length + 1
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        return "\\\\" + final[8:]
    return final[4:] if final.startswith("\\\\?\\") else final


def _hold_directory(path: Path) -> int:
    """Open `path` ONCE as a directory and return a descriptor that keeps that very directory.

    Windows: CreateFileW for listing and reading attributes, shared for reading and writing but NOT for deleting, so
    the folder cannot be renamed or replaced while the handle is held; opened as the reparse point itself (a junction
    or link is not followed), and wrapped into a descriptor so `os.fstat` describes it.  POSIX: `O_DIRECTORY` and
    `O_NOFOLLOW`.  Any failure is an OSError: there is no fallback to checks on the path."""
    if sys.platform != "win32":
        if not _HAS_DIR_FD:
            raise OSError("this system cannot open files relative to a directory descriptor")
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    import ctypes
    import msvcrt

    kernel = _kernel32()
    # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES; FILE_SHARE_READ | FILE_SHARE_WRITE; OPEN_EXISTING;
    # FILE_FLAG_BACKUP_SEMANTICS (a directory can be opened) | FILE_FLAG_OPEN_REPARSE_POINT (do not follow a link)
    handle = kernel.CreateFileW(os.fspath(path), 0x1 | 0x80, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


class _NotPlain(Exception):
    """The held object is not a plain folder (a link or a reparse point, or not a directory)."""


class _Folder:
    """The record folder, opened ONCE; that held object is the baseline for everything read from it.

    `verify` checks the path with `lstat`, opens the folder once (`_hold_directory`) and takes the baseline from the
    HELD object: its device, file id and mode (`os.fstat`) and, on Windows, its own final path.  The handle stays
    open until `close` (Windows: without delete sharing, so the folder cannot be renamed or replaced meanwhile).
    On POSIX the journals are listed and opened relative to the held descriptor, so no path is resolved again.
    After each open `unchanged` looks at the folder path once more, and `holds` asks (Windows) whether the open
    file's own final path is that name inside the held folder."""

    def __init__(self, path: Path, info: Any, fd: int) -> None:
        self.path = path
        self.fd: int | None = fd
        self.dir_fd: int | None = fd if _HAS_DIR_FD else None  # POSIX: names are resolved against this descriptor
        held = os.fstat(fd)
        if not stat.S_ISDIR(held.st_mode) or _is_reparse_point(held):
            raise _NotPlain
        self.identity = _identity(held)
        if self.identity[:2] != _identity(info)[:2]:
            raise OSError("the record folder is not the one that was checked")
        self.real = _final_path(fd) if sys.platform == "win32" else ""  # the handle's own path, not a second lookup

    @classmethod
    def verify(cls, path: Path, problems: _Problems) -> _Folder | None:
        try:
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None  # nothing there: nothing to read
        except OSError as error:  # it is there but cannot be examined: unreadable evidence is never silence
            problems.add("folder-unreadable", f"the record folder could not be examined: {error}")
            return None
        plain = "the record folder must be a plain folder, not a link or a reparse point"
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            problems.add("folder-not-plain", plain)
            return None
        try:
            fd = _hold_directory(path)
        except OSError as error:
            problems.add("folder-not-held", f"the record folder could not be opened and held: {error}")
            return None
        try:
            return cls(path, info, fd)
        except _NotPlain:
            problems.add("folder-not-plain", plain)
        except OSError as error:
            problems.add("folder-changed", f"the record folder is not the one that was checked: {error}")
        os.close(fd)
        return None

    def unchanged(self) -> bool:
        """The folder path still names the held folder: the same device, file id and mode, and not a link."""
        try:
            now = os.lstat(self.path)
        except OSError:
            return False
        return stat.S_ISDIR(now.st_mode) and not _is_reparse_point(now) and _identity(now) == self.identity

    def holds(self, fd: int, name: str) -> bool:
        """The open file `fd` is `name` inside the held folder, by the handle's own path (Windows); true where the file
        was opened relative to the folder's descriptor, which cannot lead anywhere else."""
        if self.dir_fd is not None or sys.platform != "win32":
            return True
        try:
            final = _final_path(fd)
        except OSError:
            return False
        return os.path.normcase(final) == os.path.normcase(os.path.join(self.real, name))

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd, self.dir_fd = self.fd, None, None
            os.close(fd)


def _is_reparse_point(info: Any) -> bool:
    """A Windows reparse point (a symbolic link or a junction), from `st_file_attributes` (zero elsewhere)."""
    return bool(getattr(info, "st_file_attributes", 0) & 1024)


def _read_journal(path: Path, problems: _Problems) -> list[dict[str, Any]] | None:
    """The journal's records, or None when the file is refused.  Opens without waiting; checks the open file and the
    folder it lies in: the folder `reduce_journals` verified and holds, or, called on its own, the path's own."""
    anchor = _FOLDER.get()
    own = None
    if anchor is None or anchor.path != path.parent:
        own = anchor = _Folder.verify(path.parent, problems)
        if anchor is None:
            return None
    try:
        return _read_journal_in(anchor, path, problems)
    finally:
        if own is not None:
            own.close()


def _read_journal_in(folder: _Folder, path: Path, problems: _Problems) -> list[dict[str, Any]] | None:
    no_wait = getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    plain = "the journal must be a plain file with a single name, not a link or a reparse point"
    # With a held folder descriptor the journal is named relative to it and no path is resolved again.
    target = path.name if folder.dir_fd is not None else path
    try:
        before = os.lstat(target, dir_fd=folder.dir_fd)  # the entry itself, not what it points to
        if not stat.S_ISREG(before.st_mode) or _is_reparse_point(before):
            problems.add("journal-not-plain", plain, file=path.name)
            return None
        with open(target, "rb", opener=lambda p, flags: os.open(p, flags | no_wait, dir_fd=folder.dir_fd)) as handle:
            info = os.fstat(handle.fileno())
            # Windows has no O_NOFOLLOW, so the open may have followed a link: the opened file must be the very file
            # the path names (device, inode and mode), and the path must still be a plain file after the open.
            after = os.lstat(target, dir_fd=folder.dir_fd)
            # The folder itself must be the one that was verified, and the open file must lie in it.
            if not (folder.unchanged() and folder.holds(handle.fileno(), path.name)):
                problems.add(
                    "folder-changed",
                    "the record folder is not the one that was checked, or the journal lies outside it; not used",
                    file=path.name,
                )
                return None
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not stat.S_ISREG(after.st_mode)
                or _is_reparse_point(after)
                or any(
                    getattr(before, key, None) != getattr(current, key, None)
                    for current in (info, after)
                    for key in ("st_dev", "st_ino", "st_mode")
                )
            ):
                problems.add("journal-not-plain", plain, file=path.name)
                return None
            data = handle.read(MAX_JOURNAL_BYTES + 1)
    except OSError as error:
        problems.add("journal-not-plain", f"cannot open the journal: {error}", file=path.name)
        return None
    if len(data) > MAX_JOURNAL_BYTES:
        problems.add("journal-too-large", f"the journal is over {MAX_JOURNAL_BYTES} bytes", file=path.name)
        return None
    # The number of lines is counted BEFORE anything is split or parsed: a file of newline bytes passes the byte limits,
    # and a record (even an unusable one) costs far more than the byte it came from.
    budget = _RECORDS_LEFT.get()  # [records still allowed across the journals of this reduction], or None
    cap = MAX_JOURNAL_RECORDS if budget is None else min(MAX_JOURNAL_RECORDS, budget[0])
    lines_in_file = data.count(b"\n")
    if lines_in_file > cap:
        problems.add(
            "journal-too-many-records",
            f"the journal has {lines_in_file} lines; at most {cap} more records are read ({MAX_JOURNAL_RECORDS} per "
            f"journal, {MAX_TOTAL_RECORDS} together), and none of it was",
            file=path.name,
            lines=lines_in_file,
        )
        return None
    # Split the BYTES on newline first and decode each complete line alone: a writer that died inside a multibyte
    # character leaves bad bytes only in the last, unterminated piece, and every complete line before it is kept.
    lines = data.split(b"\n")
    if lines.pop():
        problems.add("torn-last-line", "the last line has no end; its process died while writing", file=path.name)
    found = []
    for number, raw in enumerate(lines, 1):
        # A line that cannot be used still takes its place (seq = its line number), so it cannot fake a sequence gap.
        unusable = {"seq": number, "kind": "unusable", "t_wall": 0.0}
        try:
            record = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            problems.add("bad-encoding", f"line {number} is not valid UTF-8", file=path.name)
            found.append(unusable)
            continue
        except (ValueError, RecursionError):
            record = None
        if not isinstance(record, dict) or not _is_int(record.get("seq")) or not isinstance(record.get("kind"), str):
            problems.add("bad-line", f"line {number} is not a record", file=path.name)
            found.append(unusable)
            continue
        bad = _field_problem(record)
        if bad is not None:
            problems.add("bad-field", f"line {number}: {record['kind']} has a bad {bad}", file=path.name)
            found.append(unusable)
            continue
        found.append(record)
    if budget is not None:
        budget[0] -= len(found)
    return found


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float is not a time
        return False


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _is_text_or_null(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_plugin_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_identity_list(value: Any) -> bool:
    keys = ("canonical_id", "param_id", "requested_group", "effective_group")
    return isinstance(value, list) and all(
        isinstance(row, dict)
        and isinstance(row.get("canonical_id"), str)
        and all(key in row and _is_text_or_null(row[key]) for key in keys)
        for row in value
    )


# field -> (check, required), for the fields every record has and for the fields the reducer reads per kind.
_COMMON_FIELDS = {
    "v": (lambda v: _is_int(v) and v == SCHEMA_VERSION, True),
    "role": (lambda v: v in ROLE_NAMES, True),
    "worker": (_is_text_or_null, False),
    "pid": (_is_int, True),
    "t_wall": (_is_number, True),
}
_EVENT_FIELDS = {
    "process-start": {
        "started_ms": (_is_int, False),
        "loadgroup": (_is_bool, False),
        "nested_token": (_is_text_or_null, False),
        "plugins": (_is_plugin_list, False),
        "run_id": (lambda v: v is None or (isinstance(v, str) and _RUN_ID.fullmatch(v) is not None), False),
    },
    "collection-finish": {
        "count": (_is_int, True),
        "raw_ids_sha256": (_is_text, True),
        "digest_algorithm": (_is_text, True),
    },
    "collection-manifest": {
        "source": (lambda v: v in ("session", "node"), True),
        "source_worker": (_is_text_or_null, True),
        "count": (_is_int, True),
        "raw_ids": (lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v), True),
        "identities": (_is_identity_list, False),
    },
    "item-start": {
        "nodeid": (_is_text, True),
        "canonical_id": (_is_text, True),
        "param_id": (_is_text_or_null, True),
        "requested_group": (_is_text_or_null, False),
        "effective_group": (_is_text_or_null, False),
        "suffix_missing": (_is_bool, False),
    },
    "item-end": {"nodeid": (_is_text, True)},
    "phase": {
        "nodeid": (_is_text, True),
        "when": (lambda v: v in PHASE_NAMES, True),
        "outcome": (lambda v: v in PHASE_OUTCOMES, True),
        "canonical_id": (_is_text_or_null, True),
        "param_id": (_is_text_or_null, True),
        "requested_group": (_is_text_or_null, True),
        "effective_group": (_is_text_or_null, True),
    },
    "collection-error": {"nodeid": (_is_text, True), "error_text": (_is_text, False)},
    "internal-error": {"error_text": (_is_text, False)},
    "worker-down": {"has_error": (_is_bool, True), "down_worker": (_is_text, False), "error_text": (_is_text, False)},
    "nested-launch": {"token": (_is_text, True)},
    "exit-status": {"exitstatus": (_is_int, True)},
    "trailer": {"complete": (_is_bool, True), "records": (_is_int, True), "exitstatus": (_is_int, True)},
}


def _role_problem(record: dict[str, Any]) -> str | None:
    """The first process-start field that does not fit the process's role, or None.  A worker names itself and may
    carry a nested token (a worker of a nested run); a nested process always has a valid token; a serial or
    controller process has neither a worker id nor a token.  Topology is decided from these fields, so a process
    that fits no role must not get as far as being classified."""
    worker, token = record.get("worker"), record.get("nested_token")
    if record["role"] == "worker":
        if not worker:
            return "worker"
        return None if token is None or _TOKEN.fullmatch(token) else "nested_token"
    if worker is not None:
        return "worker"
    if record["role"] == "nested":
        return None if token is not None and _TOKEN.fullmatch(token) else "nested_token"
    return None if token is None else "nested_token"


def _field_problem(record: dict[str, Any]) -> str | None:
    """The name of the first damaged field of `record`, or None."""
    if record["kind"] not in EVENT_KINDS:
        return "kind"
    for fields in (_COMMON_FIELDS, _EVENT_FIELDS.get(record["kind"], {})):
        for name, (check, required) in fields.items():
            if name not in record:
                if required:
                    return name
            elif not check(record[name]):
                return name
    if record["kind"] == "process-start":
        return _role_problem(record)
    if record["kind"] == "collection-manifest":
        if record["count"] != len(record["raw_ids"]):
            return "count"
        if record["source"] == "session" and len(record.get("identities", ())) != len(record["raw_ids"]):
            return "identities"
    return None


_PHASE_RANK = {"setup": 0, "call": 1, "teardown": 2}


def _phase_problem(phases: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(problem kind, detail) when the phases of an ended item are not what pytest gives it, else None.

    pytest runs setup, then call only if setup passed, then teardown, each once and in that order, and reports each
    of them; a missing report is lost evidence, an extra or misplaced one is a corrupt record."""
    whens = [when for when, _ in phases]
    ranks = [_PHASE_RANK.get(when) for when in whens]
    if None in ranks or any(a >= b for a, b in zip(ranks, ranks[1:], strict=False)):
        return "phase-order", f"the phases came as {whens}, not setup, call, teardown once each in that order"
    setup = dict(phases).get("setup")
    if "call" in whens and setup is not None and setup != "passed":
        return "phase-order", f"a call phase came after a setup that {setup}"
    missing = [when for when in ("setup", "call", "teardown") if when not in whens]
    if "call" in missing and setup != "passed":
        missing.remove("call")  # a setup that did not pass has no call; a missing setup is reported on its own
    if missing:
        return "phase-missing", f"no {', '.join(missing)} phase was recorded"
    return None


def _identity_fits(raw: str, identity: dict[str, Any]) -> bool:
    """The identity a session manifest pairs with `raw` follows from it: the canonical id is the raw id minus its
    group suffix, and the effective group is the requested group exactly when the raw id ends with its suffix."""
    group = identity["requested_group"]
    suffixed = group is not None and raw.endswith("@" + group)
    return identity["canonical_id"] == canonical_id(raw, group) and identity["effective_group"] == (
        group if suffixed else None
    )


def _check_manifest_ids(record: dict[str, Any], name: str, problems: _Problems, extra: dict[str, Any]) -> None:
    """A manifest lists each id once; a session manifest pairs each raw id with an identity that follows from it."""
    raw_ids = record["raw_ids"]
    named = [raw for raw, count in Counter(raw_ids).items() if count > 1]
    if record["source"] == "session":
        for raw, identity in zip(raw_ids, record["identities"], strict=True):
            if not _identity_fits(raw, identity):
                problems.add(
                    "manifest-identity-mismatch",
                    "the identity paired with a raw id is not the one that raw id gives",
                    nodeid=raw,
                    file=name,
                    **extra,
                )
        named += [
            canonical
            for canonical, count in Counter(i["canonical_id"] for i in record["identities"]).items()
            if count > 1 and canonical not in named
        ]
    for nodeid in named:
        problems.add(
            "manifest-duplicate-id",
            "the manifest lists this id more than once, so one start cannot account for it",
            nodeid=nodeid,
            file=name,
            source=record["source"],
            **extra,
        )


def _identity_difference(expected: dict[str, Any], record: dict[str, Any]) -> str | None:
    """The first identity field of `record` that is not what `expected` says (a missing field counts as null)."""
    for key in IDENTITY_FIELDS:
        if record.get(key) != expected.get(key):
            return key
    return None


def _phase_check(phases: list[tuple[str, str]], require_phases: bool) -> tuple[str, str] | None:
    """The phase problem of an item, if any. A no-body probe (`require_phases` off) must have NO phase at all, so any
    phase it reports is a problem of its own kind (a body ran by mistake, or the phases are not a probe's)."""
    if require_phases:
        return _phase_problem(phases)
    if phases:
        whens = [when for when, _ in phases]
        return "phase-in-no-body-probe", f"a probe run that runs no test bodies reported phases {whens}"
    return None


def _listed_by_path(folder: Path) -> tuple[list[Path], int]:
    """The journal names in `folder` and their total size, from the ENTRIES themselves (`lstat`): a link or a reparse
    point is listed but never followed, so a target that is unavailable, or whose size is not the entry's, cannot
    block the reducer or decide a limit; the open that comes later refuses the entry."""
    paths = [folder / name for name in sorted(os.listdir(folder)) if fnmatch.fnmatch(name, "journal-*.jsonl")]
    total = 0
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            continue  # reported when the journal is opened
        if not _is_reparse_point(info):
            total += info.st_size
    return paths, total


def _journal_paths(folder: Path, problems: _Problems) -> tuple[_Folder | None, list[Path]]:
    """The verified folder and the journals to read, or (None, []): the record folder must be a plain folder (not a
    link or a reparse point), and the journals together must fit the limits, checked here, before any is parsed."""
    anchor = _Folder.verify(folder, problems)
    if anchor is None:
        return None, []
    try:
        total = 0
        if anchor.dir_fd is not None:  # POSIX: the inventory comes from the held directory itself
            names = sorted(n for n in os.listdir(anchor.dir_fd) if fnmatch.fnmatchcase(n, "journal-*.jsonl"))
            paths = [folder / name for name in names]
            for name in names:
                try:
                    total += os.stat(name, dir_fd=anchor.dir_fd, follow_symlinks=False).st_size
                except OSError:
                    pass  # reported when the journal is opened
        else:  # Windows: the names come from the path; every open is checked against the held handle
            paths, total = _listed_by_path(folder)
        if len(paths) > MAX_JOURNALS or total > MAX_TOTAL_JOURNAL_BYTES:
            problems.add(
                "input-too-large",
                f"{len(paths)} journals of {total} bytes; at most {MAX_JOURNALS} journals and "
                f"{MAX_TOTAL_JOURNAL_BYTES} bytes are read, and none was",
                journals=len(paths),
                total_bytes=total,
            )
            anchor.close()
            return None, []
        return anchor, paths
    except OSError as error:  # EACCES, EIO, ...: a named refusal, like the stat and open failures
        anchor.close()
        problems.add("folder-unreadable", f"the record folder is held but its entries could not be listed: {error}")
        return None, []
    except BaseException:
        anchor.close()  # owned from the moment it was opened: any exception during the listing releases it
        raise


def reduce_journals(
    folder: str | os.PathLike[str],
    expected_ids: Sequence[str] | None = None,
    nested_seen: int | None = None,
    *,
    require_phases: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Join the journals in `folder` (see the module docstring for the result)."""
    if run_id is not None and not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)):
        raise ValueError("run_id must be 1 to 64 letters, digits, dots, dashes or underscores")
    problems = _Problems()
    processes: list[dict[str, Any]] = []
    loaded: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    anchor, paths = _journal_paths(Path(folder), problems)
    token = _FOLDER.set(anchor)  # every journal is opened against the one folder that was verified
    try:
        budget_token = _RECORDS_LEFT.set([MAX_TOTAL_RECORDS])
        try:
            journals = [(path, _read_journal(path, problems)) for path in paths]
        finally:
            _RECORDS_LEFT.reset(budget_token)
    finally:
        _FOLDER.reset(token)
        if anchor is not None:
            anchor.close()
    for path, recs in journals:
        if recs is None:
            processes.append(
                {
                    "file": path.name,
                    "role": None,
                    "worker": None,
                    "pid": None,
                    "complete": False,
                    "records": 0,
                    "plugins": None,
                    "exitstatus": None,
                    "nested_token": None,
                    "run_id": None,
                }
            )
            continue
        start = next((r for r in recs if r["kind"] == "process-start"), None)
        trailers = [i for i, r in enumerate(recs) if r["kind"] == "trailer"]
        trailer = recs[-1] if trailers == [len(recs) - 1] and recs[-1].get("complete") is True else None
        named = re.match(r"journal-([a-z]+)-", path.name)
        info = {
            "file": path.name,
            "role": (start or {}).get("role") or (named.group(1) if named and named.group(1) in ROLE_NAMES else None),
            "worker": (start or {}).get("worker"),
            "pid": (start or {}).get("pid"),
            "complete": trailer is not None,
            "records": len(recs),
            "plugins": (start or {}).get("plugins"),
            "exitstatus": (trailer or {}).get("exitstatus"),
            "nested_token": (start or {}).get("nested_token"),
            "run_id": (start or {}).get("run_id"),
        }
        processes.append(info)
        loaded.append((path.name, info, recs))
        if run_id is not None and info["run_id"] != run_id:
            problems.add(
                "run-mismatch",
                f"the journal belongs to run {info['run_id']!r}, not to the run {run_id!r} that is being reduced: "
                "it is evidence of an earlier run",
                file=path.name,
            )
        if [r["seq"] for r in recs] != list(range(1, len(recs) + 1)):
            problems.add("sequence-gap", "the sequence numbers are not 1, 2, 3, ...", file=path.name)
        if start is None or recs[0] is not start:
            problems.add("no-process-start", "the journal does not begin with process-start", file=path.name)
        elif not isinstance(start.get("plugins"), list):
            problems.add("plugin-inventory-missing", "process-start has no plugins field", file=path.name)
        if not trailers:
            problems.add("no-trailer", "the journal has no completion trailer", file=path.name)
        elif trailer is None:
            problems.add(
                "records-after-trailer", "the trailer is not the last record, or is not complete", file=path.name
            )
        elif trailer["records"] != len(recs) - 1:
            problems.add(
                "trailer-count-mismatch",
                f"the trailer counts {trailer['records']} records before it; there are {len(recs) - 1}",
                file=path.name,
            )
        if trailer is not None and any(
            r["kind"] == "exit-status" and r.get("exitstatus") != trailer.get("exitstatus") for r in recs
        ):
            problems.add(
                "exit-status-mismatch", "the exit-status event and the completion trailer disagree", file=path.name
            )

    roots = [
        info for _, info, _ in loaded if not info["nested_token"] and info["role"] in ("serial", "controller", "worker")
    ]
    serials = [r for r in roots if r["role"] == "serial"]
    controllers = [r for r in roots if r["role"] == "controller"]
    worker_ids = Counter(r["worker"] for r in roots if r["role"] == "worker")
    if len(serials) > 1 or len(controllers) > 1 or any(count > 1 for count in worker_ids.values()):
        problems.add(
            "multiple-root-runs",
            f"the record folder holds more than one root run: {len(serials)} serial journals, {len(controllers)} "
            f"controller journals, worker ids used more than once: {sorted(w for w, c in worker_ids.items() if c > 1)}",
        )
    if serials and (controllers or worker_ids):
        problems.add("mixed-root-topology", "the record folder holds a serial run and an xdist run together")
    nested = [info for _, info, _ in loaded if info["nested_token"] and info["role"] in ("nested", "worker")]
    nested_roles = Counter(i["nested_token"] for i in nested if i["role"] == "nested")
    nested_workers = Counter((i["nested_token"], i["worker"]) for i in nested if i["role"] == "worker")
    twice = {t for t, count in nested_roles.items() if count > 1} | {t for (t, _), c in nested_workers.items() if c > 1}
    for token in sorted(twice):
        problems.add(
            "multiple-nested-runs",
            "more than one nested journal, or one worker id twice, was written under one nested token: two runs "
            "launched with one handoff cannot be told apart",
            nested_run=token,
        )

    items: dict[str, dict[str, Any]] = {}
    groups: dict[str, set[str]] = {}
    errors: list[tuple[tuple[float, str, int], dict[str, Any]]] = []
    failures: list[tuple[tuple[float, str, int], dict[str, Any]]] = []
    launches: list[str] = []
    journaled: set[str] = set()
    # An item that started and never ended: (journal, role, worker, nested run or None, raw id, phases seen).
    unfinished: list[tuple[str, str, str | None, str | None, str, list[tuple[str, str]]]] = []
    crashed_workers: set[tuple[str | None, str]] = set()  # (nested run or None, worker id) with a worker-down error
    crashed_ids: set[tuple[str | None, str]] = set()  # (nested run or None, raw id) of a controller's "???" report
    crash_reports: list[tuple[str | None, str, str, bool]] = []  # (nested run, raw id, journal, from a controller)
    own_sessions: dict[str | None, dict[str, str]] = defaultdict(dict)  # run -> worker id -> its session manifest
    launched_by: dict[str, list[tuple[str | None, Any]]] = defaultdict(list)  # token -> (launching run, its pid)
    nested_parents: dict[str, set[Any]] = defaultdict(set)  # nested run -> the parent_pid its journals name
    nested_starts: Counter[tuple[str, str]] = Counter()  # (nested run, canonical id) -> item starts
    collected: dict[str, None] = {}  # canonical ids, in the order they first appear in a manifest
    identity_of: dict[tuple[str | None, str], dict[str, Any]] = {}  # (nested run, raw id) -> identity, from manifests
    # Per run (None: the root run; else the nested run's token): worker id -> the raw ids its own manifest lists, and
    # worker id -> the raw ids the run's controller recorded for it.
    own_manifests: dict[str | None, dict[str, list[str]]] = defaultdict(dict)
    node_manifests: dict[str | None, dict[str, list[str]]] = defaultdict(dict)
    worker_runs: set[str | None] = set()  # runs with worker journals: they were xdist runs
    controller_runs: set[str | None] = set()  # runs with a controller journal (a nested one has the role "nested")
    nested_collected: dict[str, dict[str, None]] = defaultdict(dict)  # nested run -> canonical ids in its manifests
    nested_started: dict[str, set[str]] = defaultdict(set)  # nested run -> canonical ids that started
    # (nested run, role, journal, started any item) of a nested process with no manifest
    unmanifested: list[tuple[str, str, str, bool]] = []
    for name, info, recs in loaded:
        if info["nested_token"]:
            journaled.add(info["nested_token"])
        run = info["nested_token"] or None  # the nested run this journal belongs to; None for the root run
        if run is not None:
            nested_parents[run].add(next((r for r in recs if r["kind"] == "process-start"), {}).get("parent_pid"))
        # Every process that runs items is reconciled, nested ones too, run by run; only the root run's items feed
        # the collection, the groups and the join with the serial reference.
        reconcile = info["role"] in ("serial", "worker", "nested")
        root = run is None and info["role"] in ("serial", "worker")
        extra = {} if run is None else {"nested_run": run}
        # the controller of a run: the root run's has the role "controller"; a nested run's has the role "nested"
        controller_journal = info["role"] == ("controller" if run is None else "nested")
        if info["role"] == "worker":
            worker_runs.add(run)
        if controller_journal:
            controller_runs.add(run)
        loadgroup = bool(next((r for r in recs if r["kind"] == "process-start"), {}).get("loadgroup"))
        open_items: dict[str, dict[str, Any]] = {}
        canonical_of: dict[str, str] = {}
        own_identity: dict[str, dict[str, Any]] = {}  # raw id -> identity, from this journal's own session manifest
        started: list[dict[str, Any]] = []  # the item-start records, judged against that manifest after the loop
        ran_as_controller = False  # a root controller journal that holds an item-start
        has_manifest = False
        for record in recs:
            kind, key = record["kind"], (float(record.get("t_wall", 0)), name, record["seq"])
            entry = {**record, "file": name}
            if kind == "nested-launch":
                launches.append(str(record.get("token")))
                launched_by[str(record.get("token"))].append((run, info["pid"]))
            elif kind == "collection-manifest":
                _check_manifest_ids(record, name, problems, extra)
                if record["source"] == "node" and controller_journal:
                    node_manifests[run][str(record["source_worker"])] = record["raw_ids"]
                elif record["source"] == "session" and reconcile:
                    for raw_id, identity in zip(record["raw_ids"], record["identities"], strict=True):
                        identity_of.setdefault((run, raw_id), identity)
                        own_identity.setdefault(raw_id, identity)
                    has_manifest = True
                    if info["role"] == "worker":
                        own_manifests[run][str(info["worker"])] = record["raw_ids"]
                        own_sessions[run][str(info["worker"])] = json.dumps(
                            [record["raw_ids"], record["identities"]], sort_keys=True
                        )
                    if root:
                        for identity in record["identities"]:
                            collected.setdefault(identity["canonical_id"])
                    elif run is not None:
                        for identity in record["identities"]:
                            nested_collected[run].setdefault(identity["canonical_id"])
            elif (
                kind == "worker-down"
                and record.get("has_error")
                or kind in ("collection-error", "internal-error")
                or (kind in ("exit-status", "trailer") and record.get("exitstatus") not in (0, 1))
            ):
                errors.append((key, entry))
                if kind == "worker-down":
                    crashed_workers.add((run, str(record.get("down_worker"))))
            elif kind == "phase":
                if record.get("outcome") == "failed":
                    failures.append((key, entry))
                if record["when"] == "???":  # the controller's report of a worker that died in this item
                    crash_reports.append((run, record["nodeid"], name, controller_journal))
                    if controller_journal:  # only a controller's report can excuse an item that never ended
                        crashed_ids.add((run, record["nodeid"]))
                    entry["nested_run"] = run
                elif reconcile:
                    current = open_items.get(record["nodeid"])
                    if current is None:
                        problems.add(
                            "phase-outside-item",
                            "a phase report came when no such item was running",
                            nodeid=record["nodeid"],
                            file=name,
                            **extra,
                        )
                    else:
                        current["phases"].append((record["when"], record["outcome"]))
                        differs = _identity_difference(current["identity"], record)
                        if differs is not None:
                            problems.add(
                                "phase-identity-mismatch",
                                f"the phase's {differs} is not the {differs} of the item that is running",
                                nodeid=record["nodeid"],
                                file=name,
                                **extra,
                            )
            elif kind == "item-start" and reconcile:
                started.append(record)
                group = record.get("requested_group")
                canonical = canonical_of[record["nodeid"]] = canonical_id(record["nodeid"], group)
                if run is not None:
                    nested_started[run].add(canonical)
                    nested_starts[(run, canonical)] += 1
                # Derived from the node id and the requested group, never trusted from the persisted flag; for every
                # process that runs items, the root run's and each nested run's.
                missing = _suffix_missing(record["nodeid"], group)
                if "suffix_missing" in record and record["suffix_missing"] != missing:
                    problems.add(
                        "item-identity-mismatch",
                        "the item's suffix_missing is not the one its node id and requested group give",
                        nodeid=record["nodeid"],
                        file=name,
                        **extra,
                    )
                if missing and loadgroup:
                    problems.add(
                        "suffix-missing",
                        "a group member has no @group suffix in its node id",
                        nodeid=record["nodeid"],
                        file=name,
                        **extra,
                    )
                if root:
                    item = items.setdefault(canonical, {"started": 0, "ended": 0, "workers": [], "group": group})
                    item["started"] += 1
                    item["workers"].append(info["worker"] or info["role"])
                    if record.get("effective_group") is not None:  # the writer's rule: an empty name is a group
                        groups.setdefault(record["effective_group"], set()).add(info["worker"] or info["role"])
                if record["nodeid"] in open_items:  # started again before it ended
                    again = open_items[record["nodeid"]]["phases"]
                    unfinished.append((name, info["role"], info["worker"], run, record["nodeid"], again))
                open_items[record["nodeid"]] = {
                    "phases": [],
                    "canonical": canonical,
                    "identity": {key: record.get(key) for key in IDENTITY_FIELDS},
                }
            elif kind == "item-start":  # not reconciled: only the root controller gets here, and it runs no item
                ran_as_controller = True
            elif kind == "item-end" and reconcile:
                current = open_items.pop(record["nodeid"], None)
                if current is None:
                    problems.add(
                        "item-end-outside-item",
                        "an item ended that was not running",
                        nodeid=record["nodeid"],
                        file=name,
                        **extra,
                    )
                    continue
                if root:
                    items[canonical_of[record["nodeid"]]]["ended"] += 1
                found_problem = _phase_check(current["phases"], require_phases)
                if found_problem is not None:
                    problems.add(found_problem[0], found_problem[1], nodeid=current["canonical"], file=name, **extra)
        # Both the manifest and the starts are known now, so where in the journal each stands does not matter.
        for record in started:
            nodeid = record["nodeid"]
            if has_manifest and nodeid not in own_identity:
                problems.add(
                    "item-not-in-manifest",
                    "an item started that this process's own collection manifest never listed",
                    nodeid=nodeid,
                    file=name,
                    **extra,
                )
            expected = {
                **own_identity.get(nodeid, record),
                "canonical_id": canonical_id(nodeid, record.get("requested_group")),
            }
            differs = _identity_difference(expected, record)
            if differs is not None:
                problems.add(
                    "item-identity-mismatch",
                    f"the item's {differs} is not the one its node id and manifest row give",
                    nodeid=nodeid,
                    file=name,
                    **extra,
                )
        for nodeid, current in open_items.items():
            unfinished.append((name, info["role"], info["worker"], run, nodeid, current["phases"]))
        if root and not has_manifest:
            problems.add("manifest-missing", "the process wrote no collection manifest", file=name)
        elif run is not None and reconcile and not has_manifest:
            unmanifested.append((run, info["role"], name, bool(started)))
        if ran_as_controller:
            problems.add(
                "controller-ran-items", "an xdist controller started an item: it runs none", file=name, **extra
            )

    for name, role, worker, run, nodeid, phases in unfinished:
        recorded = (run, nodeid) in crashed_ids or (worker is not None and (run, worker) in crashed_workers)
        extra = {} if run is None else {"nested_run": run}
        if phases and not require_phases:
            problems.add("phase-in-no-body-probe", _phase_check(phases, False)[1], nodeid=nodeid, file=name, **extra)
        problems.add(
            "item-unfinished",
            "an item started and never ended",
            nodeid=nodeid,
            file=name,
            phases_seen=[when for when, _ in phases],
            crash_recorded=recorded,
            **extra,
        )
        if role == "worker" and not recorded:
            problems.add(
                "crash-unrecorded",
                "a worker's item never ended and no crash record names it or its worker",
                nodeid=nodeid,
                **extra,
            )
    for _key, failed in failures:
        if failed["when"] == "???":
            # the join to the manifest of the same run, for a crash report that has no item behind it
            failed["resolved"] = identity_of.get((failed.pop("nested_run", None), failed["nodeid"]))

    for group, workers in sorted(groups.items()):
        if len(workers) > 1:
            problems.add("group-split", f"members of group {group} ran on {sorted(workers)}", group=group)
    if expected_ids is not None:
        expected = Counter(expected_ids)
        for canonical, item in sorted(items.items()):
            if canonical not in expected:
                problems.add("unexpected-id", "started but not in the serial collection", nodeid=canonical)
            elif item["started"] > 1:
                problems.add("duplicate-id", f"started {item['started']} times", nodeid=canonical)
        for canonical in sorted(expected):
            if canonical not in items:
                problems.add("missing-id", "in the serial collection but never started", nodeid=canonical)
    never_started = [canonical for canonical in collected if canonical not in items]
    for canonical in never_started:
        problems.add("never-started", "collected but never started", nodeid=canonical)
    if collected and expected_ids is not None and set(collected) != set(expected_ids):
        only_collected = sorted(set(collected) - set(expected_ids))[:5]
        only_expected = sorted(set(expected_ids) - set(collected))[:5]
        problems.add(
            "collection-mismatch",
            "the collection differs from the serial reference",
            only_collected=only_collected,
            only_reference=only_expected,
        )
    # A nested serial run and a nested worker must have written a manifest. The "nested" journal of a nested xdist run
    # is its controller, which collects nothing and needs none ONLY while it starts no item: a controller that ran
    # items without saying what it collected is refused like any other process.
    for run, role, name, ran in unmanifested:
        if role == "worker" or run not in worker_runs or ran:
            detail = (
                "the process started items but wrote no collection manifest"
                if ran
                else "the process wrote no collection manifest"
            )
            problems.add("manifest-missing", detail, file=name, nested_run=run)
    listed = {run: {raw for ids in nodes.values() for raw in ids} for run, nodes in node_manifests.items()}
    for run, nodeid, name, from_controller in crash_reports:
        extra = {} if run is None else {"nested_run": run}
        if not from_controller:
            problems.add(
                "crash-report-outside-controller",
                "a crash report is written only by a controller; this journal is not one",
                nodeid=nodeid,
                file=name,
                **extra,
            )
        elif nodeid not in listed.get(run, ()):
            problems.add(
                "crash-report-unknown-item",
                "the controller reports a crash in an item that none of its node manifests lists",
                nodeid=nodeid,
                file=name,
                **extra,
            )
    for (run, canonical), count in sorted(nested_starts.items()):
        if count > 1:
            problems.add("duplicate-id", f"started {count} times", nodeid=canonical, nested_run=run)
    if None in controller_runs and None not in worker_runs and not node_manifests.get(None):
        problems.add(
            "worker-evidence-missing",
            "a controller journal shows an xdist run, and there is neither a worker journal nor a node manifest",
        )
    for run in sorted(worker_runs | set(node_manifests), key=lambda r: r or ""):
        extra = {} if run is None else {"nested_run": run}
        if run in worker_runs and run not in controller_runs:
            problems.add(
                "controller-missing",
                "worker journals show an xdist run, and there is no controller journal (nor its node manifests)",
                **extra,
            )
            continue
        own, nodes = own_manifests.get(run, {}), node_manifests.get(run, {})
        if len(set(own_sessions.get(run, {}).values())) > 1:  # xdist workers collect one common ordered list
            problems.add(
                "workers-collected-differently",
                "the workers of this run did not all collect the same ordered list of items",
                workers=sorted(own_sessions[run]),
                **extra,
            )
        for worker, ids in sorted(own.items()):
            if nodes.get(worker) != ids:
                problems.add(
                    "manifest-disagreement",
                    "the controller's manifest for a worker is missing or differs from the worker's own",
                    worker=worker,
                    **extra,
                )
        for worker in sorted(set(nodes) - set(own)):
            problems.add(
                "manifest-disagreement",
                "the controller recorded a worker that wrote no manifest",
                worker=worker,
                **extra,
            )
    for run in sorted(nested_collected):
        for canonical in nested_collected[run]:
            if canonical not in nested_started[run]:
                problems.add("never-started", "collected but never started", nodeid=canonical, nested_run=run)
    # A nested run is reachable when a launch record in a reachable run, written by the very process its journals name
    # as their parent, launched its token; the root run is reachable by definition.
    reachable: set[str | None] = {None}
    grown = True
    while grown:
        grown = False
        for token in sorted(journaled - reachable):
            parents = nested_parents[token]
            if parents and all(
                any(pid is not None and pid == parent and run in reachable for run, pid in launched_by.get(token, ()))
                for parent in parents
            ):
                reachable.add(token)
                grown = True
    for token in sorted((journaled & set(launched_by)) - reachable):
        problems.add(
            "nested-run-unreachable",
            "no root process launched this nested run: the launch record is not in a reachable run, or is not from "
            "the process its journals name as their parent",
            token=token,
        )
    unlaunched = journaled - set(launches)
    for token in sorted(unlaunched):
        problems.add("nested-journal-unlaunched", "a nested journal has no launch record", token=token)
    for token in sorted(set(launches) - journaled):
        problems.add("nested-journal-missing", "a nested run was launched but left no journal", token=token)
    if nested_seen is not None and nested_seen > len(journaled):
        problems.add("nested-journal-missing", f"{nested_seen} nested sessions were seen, {len(journaled)} journaled")

    first_error = min(errors, key=lambda pair: pair[0])[1] if errors else None
    first_failure = min(failures, key=lambda pair: pair[0])[1] if failures else None
    status = "complete" if processes and all(p["complete"] for p in processes) else "incomplete"
    return {
        "schema": SCHEMA_VERSION,
        "status": status,
        "accepted": status == "complete" and not problems.counts and first_error is None,
        "problems": problems.listed,
        "problem_counts": dict(problems.counts),
        "first_error": first_error,
        "first_failure": first_failure,
        "processes": processes,
        "items": items,
        "groups": {g: sorted(w) for g, w in groups.items()},
        "collection": {
            "collected": len(collected) if collected else None,
            "never_started": never_started,
            "nodes": {worker: len(ids) for worker, ids in sorted(node_manifests.get(None, {}).items())},
        },
        "nested": {"launched": len(launches), "journaled": len(set(launches) & journaled)},
    }
