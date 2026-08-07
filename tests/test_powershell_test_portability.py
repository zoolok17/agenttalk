from __future__ import annotations

import ast
from pathlib import Path


TESTS = Path(__file__).resolve().parent

EXPECTED_WINDOWS_ONLY = {
    "test_powershell_cli.py": {
        "test_start_host_failure_occurs_before_server_bind",
        "test_start_validates_before_bind_and_launches_absolute_selected_host",
        # These specify the Windows-only supervisor auto-start branch. Running
        # them elsewhere would simulate the OS, not cover a portable path.
        "test_start_contains_child_when_identity_capture_fails",
        "test_start_does_not_report_unclaimed_spawn_pid",
        "test_start_precondition_precedes_explicit_host_selection",
        "test_start_refuses_dead_marker_before_server_bind_or_spawn",
        "test_start_terminates_an_alive_child_that_never_claimed",
    },
    "test_powershell_host.py": {
        "test_automatic_candidates_continue_in_native_order",
        "test_automatic_candidate_cannot_redirect_outside_program_files",
        "test_candidate_path_rejects_mapped_drive_before_probe",
        "test_candidate_path_rejects_windows_shapes_before_identity",
        "test_selection_validation_rejects_ineligible_serialized_paths",
    },
    "test_supervisor_lifecycle.py": {
        "test_explicit_selection_repairs_invalid_record_under_writer_locks",
        "test_lifecycle_barrier_blocks_claim_select_and_refresh_without_mutation",
        "test_task_install_commit_refuses_concurrent_selection_change",
        "test_task_install_prepare_refuses_different_existing_binding",
        "test_task_uninstall_clears_binding_before_new_name_prepare",
        "test_validate_ancestry_accepts_direct_and_cmd_hop",
        # Exact FILETIME pairs are a Windows process-identity contract.
        "test_repair_marker_refuses_inconsistent_exact_identity_fields",
        # Toolhelp32 enumeration and its WinError semantics only exist on Windows.
        "test_process_parent_map_rejects_partial_snapshot",
        # Exact marker-owner stopping is intentionally a Windows-only command.
        "test_stop_instance_requires_kill_switch_without_mutating_marker",
        # This exercises GetProcessTimes and termination through one Win32 handle.
        "test_stop_instance_verifies_creation_time_on_the_terminated_handle",
    },
}


def _decorator_name(decorator: ast.expr) -> str | None:
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name):
        return decorator.func.id
    return None


def _windows_only_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_decorator_name(item) == "WINDOWS_ONLY" for item in node.decorator_list)
    }


def _requires_known_process_start_token(node: ast.AST) -> bool:
    token_names = {
        target.id
        for item in ast.walk(node)
        if isinstance(item, ast.Assign)
        and any(
            isinstance(descendant, ast.Call)
            and isinstance(descendant.func, ast.Name)
            and descendant.func.id == "_process_start_token"
            for descendant in ast.walk(item.value)
        )
        for target in item.targets
        if isinstance(target, ast.Name)
    }
    for item in ast.walk(node):
        if not isinstance(item, ast.Assert) or not isinstance(item.test, ast.Compare):
            continue
        comparison = item.test
        if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.IsNot):
            continue
        operands = (comparison.left, *comparison.comparators)
        has_none = any(
            isinstance(operand, ast.Constant) and operand.value is None
            for operand in operands
        )
        has_token = any(
            isinstance(operand, ast.Name) and operand.id in token_names
            for operand in operands
        )
        if has_none and has_token:
            return True
    return False


def test_mixed_powershell_modules_guard_only_windows_specific_tests() -> None:
    actual = {
        filename: _windows_only_functions(TESTS / filename)
        for filename in EXPECTED_WINDOWS_ONLY
    }
    assert actual == EXPECTED_WINDOWS_ONLY


def test_portable_powershell_tests_allow_unknown_process_start_tokens() -> None:
    paths = sorted(TESTS.glob("test_powershell_*.py")) + [
        TESTS / "test_supervisor_lifecycle.py",
    ]
    violations: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                _decorator_name(item) == "WINDOWS_ONLY"
                for item in node.decorator_list
            ):
                continue
            if _requires_known_process_start_token(node):
                violations.add(f"{path.name}::{node.name}")
    assert violations == set()


def test_real_powershell_module_skips_only_outside_win32() -> None:
    path = TESTS / "test_powershell_functional.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    marker = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        )
    )
    assert isinstance(marker, ast.Call)
    condition = marker.args[0]
    assert isinstance(condition, ast.Compare)
    assert isinstance(condition.left, ast.Attribute)
    assert isinstance(condition.left.value, ast.Name)
    assert (condition.left.value.id, condition.left.attr) == ("sys", "platform")
    assert len(condition.ops) == 1 and isinstance(condition.ops[0], ast.NotEq)
    assert len(condition.comparators) == 1
    assert isinstance(condition.comparators[0], ast.Constant)
    assert condition.comparators[0].value == "win32"


def test_powershell_runtime_modules_avoid_top_level_windows_only_imports() -> None:
    for relative in (
        Path("src/agenttalk/powershell_host.py"),
        Path("src/agenttalk/supervisor_lifecycle.py"),
    ):
        path = TESTS.parent / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        assert imported.isdisjoint({"msvcrt", "winreg", "win32api", "win32file"})
