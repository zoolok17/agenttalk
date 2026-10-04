"""A recorded child-turn scenario on a child-cap schema-3 ledger.

The same file runs against agenttalk master c80e1e5 (the code before quota lease
binding) to make ``tests/golden/gateway_schema3_c80e1e5.json``, and against the
current code to prove that a schema-3 ledger still behaves exactly as before: every
call's result or refusal, and the final ``status()``. It uses only calls that existed
on master. The capability tokens are random, so a credential is recorded without
its token.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import timedelta

from agenttalk import ovh_gateway as gateway
from agenttalk.ovh_gateway import MODEL_ALIAS

ISSUER = "atgw-" + "i" * 43
#: The call ceiling used by the scenario (set before the ledger is made, so the stored
#: policy hash uses it too), small enough to reach.
MAX_CALLS = 2


def _step(record: list, name: str, call):
    try:
        value = call()
    except gateway.GatewayError as exc:
        record.append([name, "raised", type(exc).__name__, str(exc)])
        return None
    if isinstance(value, gateway.ChildTurnCredential):
        shown = {"agent": value.agent, "message_id": value.message_id, "expires_at": value.expires_at}
    elif is_dataclass(value):
        shown = asdict(value)
    else:
        shown = value
    record.append([name, "returned", shown])
    return value


def run(ledger, clock) -> list:
    record: list = []

    def open_turn(message_id: str, request_id: str = ""):
        return ledger.open_child_turn(
            agent="qwen-dev-1", message_id=message_id, request_id=request_id, issuer_token=ISSUER
        )

    def close_turn(message_id: str):
        return ledger.close_child_turn(
            agent="qwen-dev-1", message_id=message_id, reason="dead_letter", issuer_token=ISSUER
        )

    a = _step(record, "open a", lambda: open_turn("msg-a", "q-a"))
    _step(record, "reserve a1", lambda: ledger.reserve_for_child("1" * 32, capability=a.token))
    _step(record, "settle a1", lambda: ledger.settle(
        "1" * 32, model=MODEL_ALIAS, input_tokens=1_000, output_tokens=100))
    _step(record, "reserve a2", lambda: ledger.reserve_for_child("2" * 32, capability=a.token))
    _step(record, "uncertain a2", lambda: ledger.mark_uncertain("2" * 32, reason="usage lost"))
    _step(record, "reconcile a2", lambda: ledger.reconcile(
        "2" * 32, outcome="charge-reserve", reason="operator charged the reserve"))
    _step(record, "reserve a3 over the call ceiling",
          lambda: ledger.reserve_for_child("3" * 32, capability=a.token))
    _step(record, "reopen a after it was capped", lambda: open_turn("msg-a", "q-a"))
    _step(record, "reopen a with another request", lambda: open_turn("msg-a", "q-other"))
    _step(record, "open b", lambda: open_turn("msg-b"))
    _step(record, "close b", lambda: close_turn("msg-b"))
    _step(record, "close b again", lambda: close_turn("msg-b"))
    _step(record, "reopen b after its close", lambda: open_turn("msg-b"))
    _step(record, "close a never opened key", lambda: close_turn("msg-never"))
    c = _step(record, "open c", lambda: open_turn("msg-c"))
    _step(record, "reserve c1", lambda: ledger.reserve_for_child("4" * 32, capability=c.token))
    _step(record, "no-send c1", lambda: ledger.reconcile(
        "4" * 32, outcome="no-send", reason="never sent"))
    _step(record, "status before expiry", ledger.status)
    clock.value += timedelta(seconds=gateway.CHILD_TURN_MAX_SECONDS + 1)
    _step(record, "reserve a after expiry", lambda: ledger.reserve_for_child("5" * 32, capability=a.token))
    _step(record, "reserve c after expiry", lambda: ledger.reserve_for_child("6" * 32, capability=c.token))
    _step(record, "reopen c after expiry", lambda: open_turn("msg-c"))
    _step(record, "status after expiry", ledger.status)
    return record
