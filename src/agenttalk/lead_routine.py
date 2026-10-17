"""The lead routine's record: what was completed, by what, and when (lead loop, slice 1).

``state/<lead>.lead-routine.json`` holds the sweep block (what the controller attempted and covered) and one entry per
part. Two kinds of writer share it, the controller's sweep and the public ``lead-routine complete``, so the file is NOT
single-writer: every update is a serialised read-modify-write under ``state/<lead>.lead-routine.lock`` (a store lock
path, which the lock inventory treats as a leaf). Atomic replacement alone prevents torn JSON, not lost updates. Each
write bumps ``generation``; a controller write that carries an OLDER ``controller_start`` than the stored one, or whose
lease is no longer the live one, is refused, so a stale controller cannot overwrite a newer controller's record.

Each part keeps TWO separate records, so neither writer can erase the other's evidence (design, section 3):
``machine`` is what a controller sweep read and covered (``kind=sweep``, with the controller's own ``controller_start``
and ``lease_id``, ``mode=managed``), and ``judgement`` is what the lead itself said it completed (``self-reported``,
with ``mode`` managed or interactive, and who). A sweep never touches ``judgement``; ``complete`` never touches
``machine``. A sweep renews a part only when every section the part needs was ATTEMPTED and none failed. Parts without
a machine half (``handover``, ``resources``, ``scheduled``) are manual: they only ever have ``judgement``, are listed
under ``manual_parts`` and are not covered by any automatic health (design, section 3).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable

from agenttalk import lead_relay
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import Store, validate_agent_name

VERSION = 2
MACHINE_PARTS = ("bus", "relay")                                     # parts with a machine half the sweep can complete
JUDGEMENT_PARTS = ("seats", "spend", "gates", "learning", "self-check")
MANUAL_PARTS = ("handover", "resources", "scheduled")                # no machine half, no automatic trigger
ALL_PARTS = MACHINE_PARTS + JUDGEMENT_PARTS + MANUAL_PARTS
KINDS = ("turn", "self-reported")
# the snapshot sections each machine part needs: all attempted and none failed, or the sweep does not renew it
MACHINE_SECTIONS = {"bus": ("threads", "dead_letters", "unrouted_escalations"), "relay": ("relay",)}


def _iso(now_epoch: float | None = None) -> str:
    dt = datetime.fromtimestamp(time.time() if now_epoch is None else now_epoch, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def record_path(store: Store, agent: str):
    return store.state_dir / ("%s.lead-routine.json" % validate_agent_name(agent))


def _lock_path(store: Store, agent: str):
    return store.state_dir / ("%s.lead-routine.lock" % validate_agent_name(agent))


def _blank() -> dict:
    return {"v": VERSION, "generation": 0, "controller_start": None, "sweep": None, "parts": {}}


def read(store: Store, agent: str) -> dict:
    """The record, degrade-safe: a missing, torn or unreadable file reads as 'nothing recorded' (a cache, not an
    authority: the obligations themselves are derived from the bus)."""
    path = record_path(store, agent)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    out = _blank()
    try:
        out["generation"] = int(data.get("generation") or 0)
    except (TypeError, ValueError):
        out["generation"] = 0
    if isinstance(data.get("controller_start"), str):
        out["controller_start"] = data["controller_start"]
    if isinstance(data.get("sweep"), dict):
        out["sweep"] = data["sweep"]
    parts = data.get("parts")
    if isinstance(parts, dict):
        for name, entry in parts.items():
            if isinstance(name, str) and isinstance(entry, dict):
                out["parts"][name] = {k: v for k, v in entry.items()
                                      if k in ("machine", "judgement") and isinstance(v, dict)}
    return out


def update(store: Store, agent: str, mutate: Callable[[dict], None], *, controller_start: str | None = None,
           still_owner: Callable[[], bool] | None = None) -> bool:
    """Serialised read-modify-write. A CONTROLLER write passes its own ``controller_start`` (and ``still_owner``,
    which re-checks inside the lock that its lease is still the live one): it is refused (False) when the stored
    record belongs to a LATER controller start or the lease has moved on. Returns True when the record was written."""
    store.state_dir.mkdir(parents=True, exist_ok=True)
    with store._exclusive_lock(_lock_path(store, agent), timeout=5.0, what="lead routine record"):
        record = read(store, agent)
        stored = record.get("controller_start")
        if controller_start is not None:
            if isinstance(stored, str) and stored > controller_start:
                return False
            if still_owner is not None and not still_owner():
                return False
            record["controller_start"] = controller_start
        mutate(record)
        record["generation"] = int(record.get("generation") or 0) + 1
        record["v"] = VERSION
        _atomic_write_text(record_path(store, agent), json.dumps(record, indent=2))
    return True


def complete(store: Store, agent: str, part: str, *, kind: str = "self-reported", covered: list[str] | None = None,
             failed: list[str] | None = None, note: str = "", now_epoch: float | None = None) -> dict:
    """Record the lead's own judgement that ``part`` was completed. Changes only this part's ``judgement``."""
    if part not in ALL_PARTS:
        raise ValueError("unknown part %r (known: %s)" % (part, ", ".join(ALL_PARTS)))
    if kind not in KINDS:
        raise ValueError("unknown kind %r" % kind)
    entry = {"at": _iso(now_epoch), "kind": kind, "by": agent,
             "mode": "managed" if store.is_managed_lead_loop(agent) else "interactive",
             "covered": list(covered or []), "failed": list(failed or [])}
    if note:
        entry["note"] = note[:200]

    def mutate(record: dict) -> None:
        record["parts"].setdefault(part, {})["judgement"] = entry

    update(store, agent, mutate)
    return entry


def record_sweep(store: Store, agent: str, *, controller_start: str, attempted: list[str], failed: list[str],
                 lease_id: str | None = None, still_owner: Callable[[], bool] | None = None,
                 now_epoch: float | None = None) -> bool:
    """The controller's write after a sweep: the sweep block, and a ``machine`` record for every machine part.

    ``controller_start`` and ``lease_id`` are the identity of the lease THIS controller acquired, carried through the
    sweep (never read back from the store at write time). A part is renewed only when every section it needs was
    attempted and none failed; otherwise its earlier ``machine`` record stays and names what failed or was not
    attempted. Returns False (nothing written) for a stale controller."""
    at = _iso(now_epoch)

    def mutate(record: dict) -> None:
        record["sweep"] = {"at": at, "attempted": list(attempted), "failed": list(failed),
                           "controller_start": controller_start, "lease_id": lease_id}
        for part, needed in MACHINE_SECTIONS.items():
            slot = record["parts"].setdefault(part, {})
            bad = [name for name in needed if name in failed]
            missing = [name for name in needed if name not in attempted and name not in failed]
            if bad or missing:
                slot["machine"] = {**slot.get("machine", {}), "failed": bad, "unattempted": missing, "failed_at": at}
            else:
                slot["machine"] = {"at": at, "kind": "sweep", "mode": "managed", "controller_start": controller_start,
                                   "lease_id": lease_id, "failed": [], "unattempted": [], "covered": list(needed)}

    return update(store, agent, mutate, controller_start=controller_start, still_owner=still_owner)


def status_field(store: Store, agent: str, *, now_epoch: float | None = None) -> dict | None:
    """The additive ``lead_routine`` field of a lead's ``status`` row, or None when there is nothing to show.

    ``parts`` maps each automatic part to its ``machine`` and ``judgement`` records (kept apart); ``manual_parts``
    maps each manual part to the lead's own record."""
    record = read(store, agent)
    try:
        obligations = lead_relay.open_obligations(lead_relay.obligations_for(store, agent))
    except Exception:  # noqa: BLE001 - advisory
        obligations = []
    now = time.time() if now_epoch is None else now_epoch
    if not record["parts"] and not record["sweep"] and not obligations:
        return None
    parts = {k: v for k, v in record["parts"].items() if k not in MANUAL_PARTS}
    manual = {k: v["judgement"] for k, v in record["parts"].items() if k in MANUAL_PARTS and "judgement" in v}
    overdue = [o for o in obligations if lead_relay.age_seconds(o, now) >= lead_relay.RELAY_DEADLINE_SECONDS]
    return {"sweep": record["sweep"], "parts": parts, "manual_parts": manual, "open_relays": len(obligations),
            "overdue_relays": len(overdue), "generation": record["generation"]}
