"""Shared fixtures for the quota lease binding tests: ledgers in each child-cap schema,
a fixed clock, and the test reference. Not a test module."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import ovh_gateway as gateway
from agenttalk.ovh_gateway import MODEL_ALIAS, SpendLedger

OPENING_EVIDENCE = "test dashboard, observed 2026-10-03T09:00:00Z"
ISSUER = "atgw-" + "i" * 43
GENERATION = "0123456789abcdef0123456789abcdef"
REFERENCE = "test-ref:0001"
OTHER_REFERENCE = "test-ref:0002"
START = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)

# child_turns exactly as child-cap schema 3 (agenttalk master c80e1e5) created it.
SCHEMA3_CHILD_TURNS = """CREATE TABLE child_turns (
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('open', 'capped', 'expired')
                ),
                max_calls INTEGER NOT NULL CHECK (max_calls > 0),
                max_micro_eur INTEGER NOT NULL CHECK (max_micro_eur > 0),
                opened_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(agent, message_id)
            )"""


class Clock:
    def __init__(self, value: datetime = START) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_ledger(tmp_path: Path, clock: Clock | None = None, **init: object) -> SpendLedger:
    """A fresh install: child-cap schema 4."""
    ledger = SpendLedger(
        tmp_path / "ledger.sqlite3", tmp_path / "install.json", now=clock or Clock()
    )
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence=OPENING_EVIDENCE,
        generation=GENERATION,
        child_cap_issuer_token=ISSUER,
        **init,
    )
    return ledger


def to_schema3(ledger: SpendLedger) -> None:
    """Turn a fresh install into the exact child-cap schema-3 shape, before any rows."""
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM child_turns").fetchone()[0] == 0
        conn.execute("DROP TABLE receipt_pending")
        conn.execute("DROP TABLE child_receipts")
        conn.execute("DROP TABLE child_turns")
        conn.execute(SCHEMA3_CHILD_TURNS)
        child_turn_max = int(
            conn.execute(
                "SELECT value FROM metadata WHERE key='child_turn_max_micro_eur'"
            ).fetchone()[0]
        )
        conn.execute("UPDATE metadata SET value='3' WHERE key='child_cap_schema_version'")
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='child_cap_policy_hash'",
            (
                gateway.child_cap_policy_hash(
                    child_turn_max_micro_eur=child_turn_max, schema_version=3
                ),
            ),
        )
        conn.execute("DELETE FROM metadata WHERE key='quota_lease_binding_required'")


def make_schema3_ledger(tmp_path: Path, clock: Clock | None = None, **init: object) -> SpendLedger:
    ledger = make_ledger(tmp_path, clock, **init)
    to_schema3(ledger)
    return ledger


def attempt(n: int) -> str:
    return f"{n:032x}"


def open_bound(ledger: SpendLedger, message_id: str, reference: str = REFERENCE, **caps: object):
    return ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id=message_id,
        request_id="",
        issuer_token=ISSUER,
        quota_lease_ref=reference,
        **caps,
    )


def open_unbound(ledger: SpendLedger, message_id: str):
    return ledger.open_child_turn(
        agent="qwen-dev-1", message_id=message_id, request_id="", issuer_token=ISSUER
    )


def close_bound(ledger: SpendLedger, message_id: str, outcome: str = "completed",
                reference: str = REFERENCE, reason: str = "done"):
    return ledger.close_child_turn(
        agent="qwen-dev-1",
        message_id=message_id,
        reason=reason,
        issuer_token=ISSUER,
        outcome=outcome,
        quota_lease_ref=reference,
    )


def settle(ledger: SpendLedger, attempt_id: str, input_tokens: int = 1_000, output_tokens: int = 100):
    return ledger.settle(
        attempt_id, model=MODEL_ALIAS, input_tokens=input_tokens, output_tokens=output_tokens
    )


def rows(ledger: SpendLedger, sql: str, *args: object) -> list[tuple]:
    with sqlite3.connect(ledger.db_path) as conn:
        return [tuple(row) for row in conn.execute(sql, args)]


def dump(ledger: SpendLedger) -> dict:
    """Every table's rows and the whole schema: a ledger that 'changed nothing'
    has the same dump."""
    with sqlite3.connect(ledger.db_path) as conn:
        schema = sorted(
            tuple(row) for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        )
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        return {
            "schema": schema,
            **{name: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {name}")) for name in tables},
        }
