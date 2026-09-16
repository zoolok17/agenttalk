"""Shared reply-construction rules for the CLI and the wrapper.

#201 (the dogfood migration's retro finding 1): a wrapped child that cannot run shell commands
cannot deliver `agenttalk reply`, so the wrapper delivers a child-written
draft file itself. The CLI and the wrapper MUST apply byte-identical
correlation-echo and digest rules or nonce dedupe silently forks (the
broadcast request_id/broadcast_id echo was the concrete divergence the #201
design review caught). This module is the single copy of those rules:
`cmd_reply` calls the same functions the wrapper does.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from agenttalk import gates as gate_mod

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agenttalk.store import Message, Store

# A reply that is ITSELF a thread-opening kind starts a NEW correlation
# thread and must not inherit the anchor's ids (aliasing two request/response
# pairs makes later responses ambiguous).
_THREAD_OPENING_REPLY_KINDS = ("review-request", "proposal")

# Draft bound mirrors the owed-action transport bound (obligations.py).
MAX_DRAFT_BYTES = 1024 * 1024


def echo_reply_correlation(
    meta: dict,
    *,
    anchor_id: str,
    anchor_meta: dict | None,
    kind: str,
) -> dict:
    """Attach the reply correlation anchors exactly as `agenttalk reply` does.

    The correlation id identifies the conversation; in_reply_to identifies
    this delivery. request_id is echoed unless the reply opens a new thread;
    broadcast_id is echoed ONLY when no request_id is present (a broadcast
    copy carries both, and the reply must echo just the request_id — echoing
    both forks the operation digest between producers of the same reply).
    Explicit caller-provided meta always wins.
    """
    anchor_meta = anchor_meta or {}
    meta["in_reply_to"] = anchor_id
    if (
        kind not in _THREAD_OPENING_REPLY_KINDS
        and "request_id" not in meta
        and "request_id" in anchor_meta
    ):
        meta["request_id"] = anchor_meta["request_id"]
    if (
        kind not in _THREAD_OPENING_REPLY_KINDS
        and "request_id" not in meta
        and "broadcast_id" not in meta
        and "broadcast_id" in anchor_meta
    ):
        meta["broadcast_id"] = anchor_meta["broadcast_id"]
    return meta


def operation_digest_for(
    meta: dict,
    *,
    operation: str,
    body: str,
    kind: str,
    recipient: str,
) -> str:
    """Canonical payload digest for one bus operation, from its FINAL meta."""
    from agenttalk.wrapper.obligations import operation_payload_digest

    return operation_payload_digest(
        operation=operation,
        body=body,
        kind=kind,
        recipient=recipient,
        in_reply_to=meta.get("in_reply_to"),
        request_id=meta.get("request_id"),
        broadcast_id=meta.get("broadcast_id"),
        origin_request_id=meta.get("origin_request_id"),
        origin_inbound_id=meta.get("origin_inbound_id"),
        origin_obligation_key_digest=meta.get("origin_obligation_key_digest"),
        expected_roster_revision=meta.get("expected_roster_revision"),
    )


def reply_draft_path(store: "Store", agent: str, inbound_id: str) -> Path:
    """The wrapper-declared draft location for one inbound message."""
    return store.state_dir / "reply-drafts" / agent / f"{inbound_id}.md"


def landed_reply_exists(store: "Store", *, agent: str, record: dict) -> bool:
    """True when a validated reply from `agent` to this record already landed.

    The dedupe guard for the two freeform channels: a capable child that ran
    `agenttalk reply` itself publishes strictly before the wrapper's
    end-of-turn check, so a match here means the wrapper must NOT publish the
    draft too. Matches by exact ``in_reply_to`` OR by the thread request_id —
    the CLI channel anchors ``--to-request`` to the LATEST thread message, so
    a reply to a nudge carrying the same request_id must also count. Both
    matches are bounded to messages AFTER this inbound (``since_id``), so an
    answer to an EARLIER question on the same thread never suppresses this
    one's draft. ``composing`` pings never close a thread and are excluded.
    """
    requester = record.get("from")
    inbound_id = record.get("id")
    if not isinstance(requester, str) or not isinstance(inbound_id, str):
        return False
    record_meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    thread_rid = record_meta.get("request_id")
    try:
        inbox = store.messages_for(requester, since_id=inbound_id)
    except Exception:
        # Reporting "already landed" on an error would LOSE the reply; the
        # open failure risks at worst a duplicate the requester can correlate
        # by in_reply_to. Prefer not losing work.
        return False
    for msg in inbox:
        if msg.sender != agent or msg.kind == "composing":
            continue
        meta = msg.meta or {}
        if meta.get("in_reply_to") == inbound_id:
            return True
        if thread_rid and meta.get("request_id") == thread_rid:
            return True
    return False


def thread_opener_kind(store: "Store", *, agent: str, request_id: str) -> str | None:
    """The kind of the message that OPENED this request_id's thread.

    Used to withhold the draft channel from a kind=message that arrives on a
    review-request/proposal thread (e.g. the author's answer to a needs-info
    review-result): the next response there must be a TYPED kind the draft
    cannot carry, and a kind=message publish would commit the turn while the
    typed response stays owed. Returns None when unknown (fail open: an
    unknown thread is treated as an ordinary message thread).
    """
    try:
        for msg in store.valid_messages():
            if (msg.meta or {}).get("request_id") == request_id:
                return msg.kind
    except Exception:
        return None
    return None


def preserve_refused_draft(draft_path: Path) -> Path | None:
    """Rename a refused draft to an observable ``.refused.md`` sibling.

    A refused draft on a turn that still commits would otherwise vanish —
    behaviorally the same silent loss #201 exists to fix. The preserved file
    keeps the child's bytes recoverable by an operator and, because the live
    draft path is now clear, can never be published by a later attempt.
    """
    target = draft_path.with_suffix(".refused.md")
    try:
        target.unlink(missing_ok=True)
        draft_path.rename(target)
    except OSError:
        return None
    return target


def refused_reason_path(draft_path: Path) -> Path:
    """The observable sidecar path for a refused draft's reason (#162).

    Derived from the LIVE draft path (before any rename), same convention as
    :func:`preserve_refused_draft` - both are simple ``with_suffix`` siblings of
    the original ``<id>.md``, so writing this sidecar never collides with the
    live draft and needs no ordering relative to the rename.
    """
    return draft_path.with_suffix(".refused.reason.txt")


def write_refused_reason(draft_path: Path, reason: str) -> Path | None:
    """Best-effort write of the refusal reason beside the draft (#162).

    Called BEFORE the draft is renamed to ``.refused.md`` - the reason sidecar
    and the preserved draft are independent siblings of the same original
    stem, so this can run in either order relative to
    :func:`preserve_refused_draft`. Never raises: a failure to record the
    reason must not turn a refusal into a crash - the ``.refused.md`` file
    (with no explanation) is still strictly better than nothing, matching
    this module's existing never-raise convention throughout.
    """
    target = refused_reason_path(draft_path)
    try:
        target.write_text(reason, encoding="utf-8")
    except OSError:
        return None
    return target


def preserve_interrupted_draft(draft_path: Path) -> Path | None:
    """Rename an interrupted attempt's leftover draft to ``.interrupted.md``.

    #202 D5: a draft cut short by a watchdog kill / crash is the child's
    recoverable progress, not stale garbage — the next attempt's rejoin context
    names this path so the child can resume instead of redoing. Single suffix;
    the target is unlinked first (Windows rename-over-existing throws —
    mirrors preserve_refused_draft). The LIVE draft path is left clear, and
    delivery reads only the exact declared live path, so the preserved copy
    can never publish.
    """
    target = draft_path.with_suffix(".interrupted.md")
    try:
        target.unlink(missing_ok=True)
        draft_path.rename(target)
    except OSError:
        return None
    return target


def read_reply_draft(draft_path: Path) -> str | None:
    """Read and bound a child-written draft; None means 'no deliverable draft'.

    read_text(encoding='utf-8') deliberately matches the CLI `--file` read
    (universal newlines) so a CRLF draft digests identically on every path.
    """
    try:
        if draft_path.is_symlink():
            return None
        if not draft_path.is_file():
            return None
        if draft_path.stat().st_size > MAX_DRAFT_BYTES:
            return None
        body = draft_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    if not body.strip():
        return None
    return body


def _classify_unreadable_draft(draft_path: Path) -> str:
    """Diagnostic-only re-derivation of which :func:`read_reply_draft` check
    failed (#162) - never the source of truth for behavior, only for the
    refusal-reason sidecar text. Order matches ``read_reply_draft`` exactly."""
    try:
        if draft_path.is_symlink():
            return "draft path is a symlink, not a plain file"
        if not draft_path.is_file():
            return "draft path is not a regular file"
        size = draft_path.stat().st_size
        if size > MAX_DRAFT_BYTES:
            return f"draft is {size} bytes, over the {MAX_DRAFT_BYTES}-byte bound"
        body = draft_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as e:
        return f"draft unreadable: {type(e).__name__}: {e}"
    if not body.strip():
        return "draft is empty (whitespace-only)"
    return "draft was readable - refusal reason undetermined"  # pragma: no cover - defensive


def deliver_draft_reply(
    store: "Store",
    *,
    agent: str,
    record: dict,
    draft_path: Path,
) -> "Message | None":
    """Validate a child-written draft and publish it as the agent's reply.

    Returns the published Message, or None when there is nothing deliverable
    (missing/oversize/empty draft, malformed record). Never raises on a
    refusal path: the caller's turn disposition must not change because
    freeform replies are not obligatory.
    """
    inbound_id = record.get("id")
    requester = record.get("from")
    if not isinstance(inbound_id, str) or not isinstance(requester, str):
        return None
    if requester == agent:
        return None
    body = read_reply_draft(draft_path)
    if body is None:
        # #162: a malformed record (checked above) is not a draft refusal - no
        # draft was ever declared to have failed. An unreadable/invalid DRAFT
        # (the only way execution reaches here with draft_path meaningful) is,
        # so it earns a reason - re-derive WHICH read_reply_draft check failed
        # for the sidecar text; read_reply_draft's own contract (a bare body-or-
        # None) is untouched, this is diagnostic-only and never changes behavior.
        if draft_path.exists():
            write_refused_reason(draft_path, _classify_unreadable_draft(draft_path))
        return None
    kind = "message"
    record_meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    meta: dict = {}
    echo_reply_correlation(
        meta, anchor_id=inbound_id, anchor_meta=record_meta, kind=kind,
    )
    nonce = secrets.token_hex(16)
    digest = operation_digest_for(
        meta, operation="terminal", body=body, kind=kind, recipient=requester,
    )
    meta["operation_nonce"] = nonce
    meta["operation_digest"] = digest
    try:
        # Parity with cmd_reply's validator step (no-ops for kind=message
        # today; inside the refusal boundary so a future typed-kind extension
        # inherits both the validators AND the never-raise contract).
        gate_mod.validate_response_status(kind, meta)
        gate_mod.validate_review_result_evidence(kind, meta)
        msg, _published = store.send_operation(
            sender=agent,
            recipient=requester,
            body=body,
            kind=kind,
            # Empty subject on purpose: byte-parity with a CLI-path reply
            # (cmd_reply defaults --subject to "").
            subject="",
            meta=meta,
            operation_nonce=nonce,
            operation_digest=digest,
        )
    except Exception as e:  # noqa: BLE001 - refusal contract: the caller preserves
        # ValueError (nonce/validator) AND operational failures (publication
        # lock timeout, I/O): returning None routes ALL of them into the
        # caller's observable refused-draft preservation instead of a silent
        # drop. In-process durable retry of publish failures is PR-2 scope
        # (the captured-operation machinery). #162: the reason is no longer
        # discarded here - write it beside the draft BEFORE returning, so the
        # caller's rename-to-.refused.md never races an unwritten sidecar.
        write_refused_reason(draft_path, f"{type(e).__name__}: {e}")
        return None
    try:
        draft_path.unlink(missing_ok=True)
    except OSError:
        pass
    return msg
