"""One-off baseline for the #923 trial (a tool, not product code; it keeps no state).

Run ONCE at activation, read-only, against the team store:

    python scripts/drift_baseline.py --root <store root> --activation 2026-12-01T00:00:00Z

It applies exactly the D1 evidence rules (`agenttalk.drift.classify`) and the same design/build/fix
scope to the 30 days before the activation instant, judging each order as of its own timestamp.
An order the rules cannot classify counts as `unverifiable`, never as `absent`. Keep the printed
output with the trial record. The product's report never makes this retrospective comparison.
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from agenttalk import drift
from agenttalk.store import Store


def baseline(msgs, activation: str) -> dict:
    end = drift._t(activation)
    if end is None:
        raise SystemExit("--activation must be an ISO 8601 UTC time")
    start, index, counts = end - timedelta(days=30), drift._by_rid(msgs), {}
    for m in msgs:
        meta, ts = m.meta or {}, drift._t(m.ts)
        if m.kind != "task" or meta.get("stage") not in drift.STAGES or ts is None or not start <= ts < end:
            continue
        try:
            cls = drift.classify(index, meta, ts)[0]
        except Exception:  # noqa: BLE001 - cannot be classified by the rules
            cls = "unverifiable"
        counts[cls] = counts.get(cls, 0) + 1
    total = sum(counts.values())
    return {"window": [start.isoformat(), end.isoformat()], "orders": total, "classes": counts,
            "method": "agenttalk.drift.classify (D1), design/build/fix tasks, as of each order's timestamp"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--activation", required=True)
    args = parser.parse_args()
    print(json.dumps(baseline(Store(Path(args.root)).valid_messages(), args.activation), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
