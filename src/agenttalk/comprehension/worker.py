"""The sanitized bundled scanner worker (DESIGN-55-comprehension-plane.md,
"System boundary" / "Privacy and offline enforcement").

    The CLI starts one bundled scanner worker with a sanitized environment.
    Adapters run in-process inside that worker and receive only an
    allowlisted input object. [...] the CLI launches the bundled worker
    without provider credentials, proxy variables, remote endpoints, or
    inherited configuration that names a network service; sanitizing the
    child environment does not mutate the parent agenttalk process.

The worker runs as a genuinely separate OS process (``python -m
agenttalk.comprehension.worker``), launched with an ALLOWLISTED environment
built by :func:`sanitized_worker_env` - never the parent's own
``os.environ`` filtered down, an allowlist, matching this codebase's
existing convention (``dev_gate._base_env``) for the same reason: an
allowlist can only omit what nobody thought to add; a blocklist can only
omit what somebody remembered to name. Input (the resolved project root and
the already-enumerated, already-confined relative paths to process) travels
over stdin as one JSON object; output (each file's default addressable-unit
claim, and one problem record per file this worker could not process)
travels over stdout as one JSON object. Neither uses an environment
variable, a shared temp file outside the caller's own control, or any IPC
mechanism that could carry ambient configuration.

Adapters dispatch INSIDE this worker (item 9), on the same bytes already
read for the default file claim - never a second, separate read, and
never outside this sanitized process boundary. The Java adapter itself
(``adapters.java``) has no knowledge of the worker/subprocess boundary at
all; it is a pure function from (path, text) to claims, so it works
identically whether called here or directly in a unit test.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404 - launches only the bundled worker module, argv fixed, shell disabled
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import java as java_adapter
from .envelope import EnvelopeError, resolve_under_root
from .errors import ComprehensionError

_ADAPTER_EXTENSIONS = {".java": java_adapter}

WORKER_SCHEMA_VERSION = 1
_WORKER_TIMEOUT_SECONDS = 300.0

#: The worker gets ONLY what a pure, offline, file-reading Python process
#: needs to start - never provider credentials, proxy variables, bus/session
#: state, or any other ambient configuration a network-capable tool could
#: use (design: "without provider credentials, proxy variables, remote
#: endpoints, or inherited configuration that names a network service").
#: Deliberately an ALLOWLIST, not a blocklist that tries to name every
#: dangerous variable - the same rationale as ``dev_gate._base_env``.
_ALLOWED_ENV_VARS = frozenset({
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "WINDIR",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "TEMP",
    "TMP",
    "TMPDIR",
})


class WorkerError(ComprehensionError):
    """The sanitized worker process could not be started, timed out, exited
    with an error, or returned output this caller cannot trust. Never
    raised for an individual file's own parse/read failure - that is a
    bounded problem record in the result instead (design: a parser failure
    yields a bounded problem, it does not erase the rest of the scan)."""

    reason_code = "comprehension_worker_error"


def sanitized_worker_env(source_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build the worker child process's environment from ``source_env``
    (the CALLER's own environment by default - never mutated in place;
    this returns a new, filtered dict) using the fixed allowlist above.
    Exposed as its own function so a test can assert exactly what the
    worker process would see without actually spawning one.
    """
    source = source_env if source_env is not None else os.environ
    return {key: value for key, value in source.items() if key.upper() in _ALLOWED_ENV_VARS}


@dataclass(frozen=True)
class WorkerFileClaim:
    """The default, adapter-independent claim for one enumerated file: it
    exists, and this is its size and content digest. Every non-excluded
    file gets exactly one of these regardless of whether any adapter
    understands it (design, Artifact 1: "Every non-excluded file remains an
    addressable `file` unit")."""

    relative_path: str
    byte_count: int
    content_digest: str


@dataclass(frozen=True)
class WorkerProblem:
    """One file this worker could not process. A preliminary, internal
    shape - the full ``problems.json`` record schema (stable ID, severity,
    producers, generated message) is formalized where that artifact is
    built; this only carries enough to construct that record later without
    losing information here."""

    reason_code: str
    relative_path: str
    detail: str


@dataclass(frozen=True)
class WorkerResult:
    schema_version: int
    file_claims: list[WorkerFileClaim] = field(default_factory=list)
    problems: list[WorkerProblem] = field(default_factory=list)
    #: relative_path -> adapters.java.file_result_to_json(...) payload, for
    #: every recognized-extension file an adapter successfully parsed.
    java_results: dict[str, dict[str, Any]] = field(default_factory=dict)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def process_paths(root: Path, relative_paths: list[str]) -> WorkerResult:
    """The worker's unit of work: read each of ``relative_paths`` under
    ``root``, produce its default file-unit claim, AND - for a recognized
    extension - dispatch the SAME already-read bytes to that language's
    bundled adapter, in-process, right here (design: "Adapters run
    in-process inside that worker"). Re-confines every path under ``root``
    itself (defense in depth - the caller has already validated and
    confined these paths during enumeration, but this process boundary
    never trusts an upstream check blindly for a security-relevant
    operation). An adapter parse failure is a bounded problem, exactly
    like an unreadable file - it never aborts the rest of the scan."""
    claims: list[WorkerFileClaim] = []
    problems: list[WorkerProblem] = []
    java_results: dict[str, dict[str, Any]] = {}
    for rel in relative_paths:
        try:
            resolved = resolve_under_root(rel, root=root, label="worker input path")
        except EnvelopeError as exc:
            problems.append(WorkerProblem(
                reason_code="path_excluded", relative_path=rel, detail=str(exc)))
            continue
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            problems.append(WorkerProblem(
                reason_code="parse_failed", relative_path=rel, detail=str(exc)))
            continue
        claims.append(WorkerFileClaim(
            relative_path=rel, byte_count=len(data), content_digest=_hash_bytes(data)))

        adapter = next(
            (mod for ext, mod in _ADAPTER_EXTENSIONS.items() if rel.endswith(ext)), None)
        if adapter is not None:
            try:
                text = data.decode("utf-8", errors="replace")
                result = adapter.parse_java_source(rel, text)
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=f"{adapter.ADAPTER_NAME} adapter failed: {exc}"))
            else:
                java_results[rel] = adapter.file_result_to_json(result)
        elif Path(rel).name == "pom.xml":
            # reviewer-3 B-3 (PR-B delta review): a pom.xml's build-relation
            # extraction used to happen in the PARENT process, reading the
            # file directly and bypassing this worker entirely - the one
            # content path where the design's "adapters run inside the
            # sanitized worker, stdin/stdout is the sole channel" boundary
            # statement did not hold. Route it through the SAME already-read
            # bytes and the SAME java_results channel every other adapter
            # claim uses, wrapped as a JavaFileResult with no units/entry
            # points of its own.
            try:
                text = data.decode("utf-8", errors="replace")
                build_edges = java_adapter.parse_maven_pom(rel, text)
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=f"{java_adapter.ADAPTER_NAME} adapter failed: {exc}"))
            else:
                java_results[rel] = java_adapter.file_result_to_json(
                    java_adapter.JavaFileResult(edges=build_edges))
        elif Path(rel).name == "web.xml":
            # M9 (cold-read, PR-B fix round 3): parse_web_xml existed as a
            # producer with its own passing unit tests but no dispatch
            # anywhere in the pipeline - the suite reported a capability
            # (servlet-mapping routes) the pipeline did not actually have.
            # The approved item-3 relation scope already names this exact
            # case ("plain-XML web.xml servlet-mapping declarations when
            # trivially present") as in-scope; wiring it in, not deleting
            # it, is what that decision calls for. Same already-read-bytes
            # / same java_results channel as pom.xml's build edges.
            try:
                text = data.decode("utf-8", errors="replace")
                web_entry_points = java_adapter.parse_web_xml(rel, text)
            except Exception as exc:  # noqa: BLE001 - a producer bug must degrade, never abort the scan
                problems.append(WorkerProblem(
                    reason_code="parse_failed", relative_path=rel,
                    detail=f"{java_adapter.ADAPTER_NAME} adapter failed: {exc}"))
            else:
                java_results[rel] = java_adapter.file_result_to_json(
                    java_adapter.JavaFileResult(entry_points=web_entry_points))

    return WorkerResult(
        schema_version=WORKER_SCHEMA_VERSION, file_claims=claims, problems=problems,
        java_results=java_results,
    )


def _result_to_json(result: WorkerResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "file_claims": [
            {
                "relative_path": claim.relative_path,
                "byte_count": claim.byte_count,
                "content_digest": claim.content_digest,
            }
            for claim in result.file_claims
        ],
        "problems": [
            {
                "reason_code": problem.reason_code,
                "relative_path": problem.relative_path,
                "detail": problem.detail,
            }
            for problem in result.problems
        ],
        # reviewer-3 B-1 (PR-B delta review): this field was previously
        # missing here entirely - the worker computed adapter claims
        # correctly, held them on its in-memory result, but never emitted
        # them across the stdout channel, so the reader always
        # reconstructed an empty dict regardless of what was actually
        # parsed. A scan run through the REAL worker therefore reported
        # `complete` while silently carrying no adapter-derived units,
        # edges, or entry points at all. Every WorkerResult field must
        # round-trip - see test_worker_result_json_round_trip_preserves_
        # every_field, the reviewer's repro made permanent.
        "java_results": result.java_results,
    }


def _result_from_json(payload: Any) -> WorkerResult:
    if not isinstance(payload, dict):
        raise WorkerError("worker output must be a JSON object")
    if payload.get("schema_version") != WORKER_SCHEMA_VERSION:
        raise WorkerError(
            f"worker output schema_version must be {WORKER_SCHEMA_VERSION}, got "
            f"{payload.get('schema_version')!r}")
    try:
        claims = [
            WorkerFileClaim(
                relative_path=item["relative_path"],
                byte_count=item["byte_count"],
                content_digest=item["content_digest"],
            )
            for item in payload["file_claims"]
        ]
        problems = [
            WorkerProblem(
                reason_code=item["reason_code"],
                relative_path=item["relative_path"],
                detail=item["detail"],
            )
            for item in payload["problems"]
        ]
        java_results = dict(payload.get("java_results", {}))
    except (KeyError, TypeError) as exc:
        raise WorkerError(f"worker output is malformed: {exc}") from exc
    return WorkerResult(
        schema_version=WORKER_SCHEMA_VERSION, file_claims=claims, problems=problems,
        java_results=java_results,
    )


def _derive_child_import_root() -> str:
    """Derive the path the CHILD needs on its module search path to import
    ``agenttalk`` exactly as THIS process resolved it - computed from this
    interpreter's OWN already-resolved package location, never inherited
    from an environment variable.

    B-2 (reviewer-3, PR-B delta review): the sanitized environment
    deliberately never allowlists ``PYTHONPATH`` - both the reviewer and
    the lead flagged it as an injection vector, since a compromised or
    merely unexpected parent environment could point it anywhere. But
    without it, the child cannot import ``agenttalk`` at all when this
    process only resolves the package via a source-tree ``PYTHONPATH``
    (not a real install), so the worker could never start from a source
    checkout. The fix is a derived, validated route instead of ambient
    inheritance: this process already knows exactly where its OWN
    ``agenttalk`` package lives (``agenttalk.__file__``); the directory
    ABOVE that package is the root the child needs. Validated with the
    same resolve-under-known-root machinery every other confined path in
    this package uses, and re-confirmed to actually contain THIS worker
    module, before it is ever handed to the child - not merely trusted
    because it was computed here.
    """
    import agenttalk

    import_root = Path(agenttalk.__file__).resolve().parent.parent
    try:
        worker_module = resolve_under_root(
            "agenttalk/comprehension/worker.py", root=import_root,
            label="derived child import root",
        )
    except EnvelopeError as exc:
        raise WorkerError(
            f"could not derive a valid child import root from this process's own "
            f"agenttalk package location ({import_root}): {exc}") from exc
    if not worker_module.is_file():
        raise WorkerError(
            f"derived child import root {import_root} does not contain "
            f"agenttalk.comprehension.worker - refusing to launch the worker from an "
            f"unverified location")
    return str(import_root)


def run_sanitized_worker(
    root: Path, relative_paths: list[str], *, timeout_seconds: float = _WORKER_TIMEOUT_SECONDS,
) -> WorkerResult:
    """Launch the worker as a child process with :func:`sanitized_worker_env`
    - never the parent's own, unfiltered ``os.environ`` - and feed it
    ``root``/``relative_paths`` over stdin/stdout only. Raises
    :class:`WorkerError` if the process cannot be started, times out, exits
    non-zero, or returns output that does not parse as a valid result;
    never raised for one file's own read/parse failure (see
    :class:`WorkerProblem`).

    The child's ``PYTHONPATH`` is set explicitly here, to a value THIS
    function derives and validates itself (:func:`_derive_child_import_root`)
    - never inherited from the caller's own environment, which
    :func:`sanitized_worker_env`'s allowlist deliberately excludes it from
    (B-2, reviewer-3, PR-B delta review)."""
    env = sanitized_worker_env()
    env["PYTHONPATH"] = _derive_child_import_root()
    payload = json.dumps({"root": str(root), "relative_paths": list(relative_paths)})
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            [sys.executable, "-m", "agenttalk.comprehension.worker"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError(f"the sanitized worker could not be started or timed out: {exc}") from exc
    if completed.returncode != 0:
        raise WorkerError(
            f"the sanitized worker exited with status {completed.returncode}: "
            f"{completed.stderr.strip()}")
    try:
        payload_out = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WorkerError(f"the sanitized worker returned malformed output: {exc}") from exc
    return _result_from_json(payload_out)


def _main(argv: list[str]) -> int:
    """The worker process's own entrypoint (``python -m
    agenttalk.comprehension.worker``). Reads one JSON object from stdin
    (``{"root": str, "relative_paths": [str, ...]}``), writes one JSON
    result object to stdout, and takes no other input - no argv beyond
    nothing, no environment variable, no network."""
    del argv
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"malformed worker input: {exc}"}), file=sys.stderr)
        return 2
    if not isinstance(payload, dict) or "root" not in payload or "relative_paths" not in payload:
        print(json.dumps({"error": "worker input must have root and relative_paths"}), file=sys.stderr)
        return 2
    result = process_paths(Path(payload["root"]), list(payload["relative_paths"]))
    sys.stdout.write(json.dumps(
        _result_to_json(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
