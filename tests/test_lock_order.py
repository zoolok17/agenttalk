"""The acceptance/bus lock order must reject inversions before waiting."""
import contextlib

import pytest

from agenttalk.store import Store


def test_retirement_cannot_nest_config(tmp_path):
    store = Store(tmp_path)
    store.init(["lead"])
    with store._retirement_lock():
        with pytest.raises(TimeoutError, match="lock order"):
            with store.config_lock(timeout=0.1):
                pass


# Concrete paths exercise the same classification as every production caller.
ORDERED_PATHS = [
    "assurance/coverage.lock", "assurance/coverage-handoff.lock",
    "supervisor-lifecycle.lock", "powershell-host.lock", "supervisor.instance.lock",
    "locks/lane-reset.lock", "locks/lane-one.transaction.lock", "state/lane-one.cleanup.lock",
    "state/operation-publication.lock", ".acceptance-write.lock", "closes/.attempt.lock",
    "config.lock", "lane-deliveries/.worktree-integrity-secret.lock", "retirement", "message-publication",
    "state/owed-action/ledger.lock", "state/owed-action/proof-health.lock",
    "state/lead.lead-loop-lease.lock", "state/lead.waiting.lock", "state/awaiting/lead.lock",
]


def _take(store, path):
    if path == "retirement":
        return store._retirement_lock(timeout=0.1)
    if path == "message-publication":
        return store._message_publication_lock(timeout=0.1)
    return store._exclusive_lock(store.dir / path, timeout=0.1)


def test_total_store_lock_order(tmp_path):
    store = Store(tmp_path)
    store.init(["lead"])
    with contextlib.ExitStack() as stack:
        for path in ORDERED_PATHS:
            stack.enter_context(_take(store, path))


@pytest.mark.parametrize("index", range(1, len(ORDERED_PATHS)))
def test_reverse_store_lock_order_is_refused(tmp_path, index):
    store = Store(tmp_path)
    store.init(["lead"])
    with _take(store, ORDERED_PATHS[index]):
        with pytest.raises(TimeoutError, match="lock order"):
            with _take(store, ORDERED_PATHS[index - 1]):
                pass


def test_package_acquisition_site_inventory():
    """A new acquisition site requires a total-order review, not a silent leaf."""
    import ast
    from collections import Counter
    from pathlib import Path
    from agenttalk import store as module

    names = {"_exclusive_lock", "_retirement_lock", "_message_publication_lock", "_config_lock",
             "config_lock", "_acceptance_writer_lock", "_supervisor_lifecycle_lock",
             "_powershell_selection_lock", "coverage_transaction_lock", "coverage_handoff_lock",
             "_waiting_lock", "_awaiting_lock", "_lead_loop_lease_lock"}
    found = Counter()
    root = Path(module.__file__).parent
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else "")
            if name in names:
                found[path.relative_to(root).as_posix()] += 1
            if name == "_exclusive_lock_unordered":
                assert path.name == "store.py", "bypassed lock rank check"
    assert found == {
        "assurance.py": 7, "attention.py": 2, "checkpoint.py": 1, "cli.py": 31,
        "close.py": 3, "gates.py": 1, "knowledge.py": 2, "lanes.py": 1,
        "lesson_context.py": 1, "onboarding.py": 2, "recovery.py": 1, "store.py": 64,
        "supervisor.py": 4, "supervisor_lifecycle.py": 14, "wrapper/obligations.py": 64,
        "wrapper/turn_watchdog.py": 1,
    }


def test_per_id_timeout_releases_acceptance_for_other_id(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agenttalk import close

    store = Store(tmp_path)
    store.init(["lead"])
    entered, release = threading.Event(), threading.Event()

    def legacy_holder():
        # Fault injection models a competing per-ID-only writer; the publishing
        # thread itself never acquires an ID before its acceptance lock.
        with store._exclusive_lock(close.closes_dir(store) / ".busy.lock", timeout=1):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(legacy_holder)
        try:
            assert entered.wait(5)
            with pytest.raises(TimeoutError):
                with close._close_update_lock(store, "busy", timeout=0.1):
                    pytest.fail("competing ID lock bypassed")
            assert not (store.dir / ".acceptance-write.lock").exists()
            with close._close_update_lock(store, "other", timeout=1):
                pass
        finally:
            release.set()
        worker.result(timeout=5)
