"""Acceptance capability dispatch covers the M1 inventory without version fallbacks."""

import ast
import os
from pathlib import Path

import pytest

from agenttalk import acceptance as A


def raw_schema_comparisons(tree):
    """Track raw version operands, including aliases; schema() consumes the taint."""
    aliases = set()
    def raw(node):
        if isinstance(node, ast.Name):
            return node.id in aliases or node.id == "schema_version"
        if isinstance(node, ast.Subscript):
            return isinstance(node.slice, ast.Constant) and node.slice.value == "schema_version"
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "schema_version")
    assignments = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assignments) + 1):
        for node in assignments:
            if raw(node.value):
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)
    return [n for n in ast.walk(tree) if isinstance(n, ast.Compare)
            and any(raw(operand) for operand in (n.left, *n.comparators))]


@pytest.mark.parametrize("source", [
    'if route["schema_version"] == 3: pass',
    'def _probe_new_reader(route):\n    return route.get("schema_version") == 3',
    'version = route.get("schema_version")\nalias = version\nif alias < 3: pass',
])
def test_inventory_detects_direct_get_and_local_alias(source):
    assert raw_schema_comparisons(ast.parse(source))


def test_known_capabilities_and_unknown_refusal():
    from agenttalk.acceptance_schema import capabilities
    assert [(capabilities(v).modern, capabilities(v).cold, capabilities(v).preflight)
            for v in (1, 2, 3, 4)] == [(False, False, False), (True, False, False),
                                      (True, True, False), (True, True, True)]
    for version in (5, 0, -1, True, 3.0, "3", None):
        with pytest.raises(A.AcceptanceError):
            capabilities(version)


def test_inventory_comparisons_use_one_refusing_dispatch():
    from agenttalk.acceptance_schema import capabilities
    root = Path(A.__file__).parent
    counts = {
        "acceptance.py": {"validate_plan": 3, "prepare": 2, "partition_lenses": 1, "freeze": 3,
                          "_route": 3, "_policy": 2, "_bundle": 2, "attach": 3, "resolve": 4, "ack_binding": 1},
        "acceptance_cold.py": {"submit": 1},
        "acceptance_obligations.py": {"_review_obligations": 1},
        "acceptance_history.py": {"successor": 3, "evaluate": 3}, "close.py": {"apply_ack": 1}}
    # 31 inventoried predicates; two compare both schema operands (33 calls).
    for filename, functions in counts.items():
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for name, minimum in functions.items():
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            actual = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                      and ((isinstance(n.func, ast.Name) and n.func.id == "schema")
                           or (isinstance(n.func, ast.Attribute) and n.func.attr == "schema"))]
            assert len(actual) >= minimum, (filename, name)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and ((isinstance(node.func, ast.Name) and node.func.id == "schema")
                      or (isinstance(node.func, ast.Attribute) and node.func.attr == "schema"))]
        assert len(calls) >= sum(functions.values()), filename
        for call in calls:
            # Execute each inventoried dispatch call with the unknown version;
            # both unqualified and A.schema aliases must be the same helper.
            target = capabilities if isinstance(call.func, ast.Name) else A.schema
            assert target is capabilities
            with pytest.raises(A.AcceptanceError):
                target(5)
    with os.scandir(root) as entries:
        files = [Path(e.path) for e in entries if
                 (e.name.startswith("acceptance") and e.name.endswith(".py")) or e.name in ("close.py", "cli.py")]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # These two close record namespaces are not acceptance plan versions.
        exceptions = {"close.py": {"_is_wellformed", "validate_dod_policy"}}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in exceptions.get(path.name, set()):
                continue
            assert not raw_schema_comparisons(node), (path.name, getattr(node, "lineno", 0))
