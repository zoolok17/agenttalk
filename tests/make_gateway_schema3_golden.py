"""Make tests/golden/gateway_schema3_c80e1e5.json from the code on PYTHONPATH.

Run it ONLY against agenttalk master c80e1e5 (child-cap schema 3, before quota lease
binding):

    git archive c80e1e5 src | tar -x -C <scratch>/master
    PYTHONPATH=<scratch>/master/src python tests/make_gateway_schema3_golden.py

See tests/gateway_schema3_scenario.py for what is recorded.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway_schema3_scenario as scenario  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "gateway_schema3_c80e1e5.json"


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


if __name__ == "__main__":
    from agenttalk import ovh_gateway as gateway

    if hasattr(gateway, "QuotaLeaseReferenceMismatch"):
        raise SystemExit("this is not master c80e1e5: the golden file records the code before binding")
    gateway.CHILD_TURN_MAX_CALLS = scenario.MAX_CALLS
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        ledger = gateway.SpendLedger(Path(tmp) / "ledger.sqlite3", Path(tmp) / "install.json", now=clock)
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence="test dashboard, observed 2026-10-03T09:00:00Z",
            generation="0123456789abcdef0123456789abcdef",
            child_cap_issuer_token=scenario.ISSUER,
        )
        record = scenario.run(ledger, clock)
    GOLDEN.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote", GOLDEN)
