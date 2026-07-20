from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import domains as dom
from agenttalk.store import Store


def _root(tmp_path: Path) -> Path:
    store = Store(tmp_path)
    store.init(["alpha", "beta", "gamma"])
    store.set_group("devs", ["alpha", "gamma"])
    store.set_role("alpha", "developer")
    store.set_role("beta", "reviewer")
    return tmp_path


def _registry() -> dict:
    return {
        "schema_version": 1,
        "domains": {
            "cli": {
                "title": "CLI",
                "owners": {"groups": ["devs"]},
                "reviewers": {"roles": ["reviewer"]},
                "curators": {"agents": ["alpha"]},
                "owned_globs": ["src/agenttalk/**/*.py", "tests/test_cli.py"],
                "description": "Command-line surface",
            },
            "core": {
                "title": "Core",
                "owners": {"agents": ["beta"]},
                "reviewers": {},
                "curators": {},
                "owned_globs": ["src/agenttalk/cli.py"],
            },
        },
        "shared_paths": [
            {
                "glob": "pyproject.toml",
                "category": "package-metadata",
                "requires": "shared-lease-or-lead-approval",
                "default_reviewers": {"roles": ["reviewer"]},
                "default_approvers": {"agents": ["alpha"]},
            }
        ],
    }


def _write_registry(root: Path, registry: dict) -> None:
    p = root / ".agenttalk" / dom.FILENAME
    p.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def test_validate_registry_resolves_refs_and_hash_is_order_stable(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = Store(root).load_config()
    first = dom.validate_registry(_registry(), cfg)
    second_raw = json.loads(json.dumps(_registry(), sort_keys=True))
    second = dom.validate_registry(second_raw, cfg)

    assert dom.resolve_refset(first["domains"]["cli"]["owners"], cfg) == ["alpha", "gamma"]
    assert dom.resolve_refset(first["domains"]["cli"]["reviewers"], cfg) == ["beta"]
    assert dom.registry_hash(first) == dom.registry_hash(second)
    assert len(dom.registry_hash(first)) == 64


def test_validate_registry_rejects_unknown_refs(tmp_path: Path) -> None:
    root = _root(tmp_path)
    registry = _registry()
    registry["domains"]["cli"]["owners"] = {"groups": ["ghosts"]}

    with pytest.raises(dom.DomainError, match="unknown group"):
        dom.validate_registry(registry, Store(root).load_config())


def test_validate_registry_rejects_duplicate_shared_glob(tmp_path: Path) -> None:
    # C2/D-11 (codex P1): two shared_paths entries with the SAME normalized glob but
    # DIFFERENT approvers would collapse the all-matching rule (the verdict keys entries
    # by glob) -> one approver silently clears the path, bypassing the other. Validation
    # must fail closed; merge into one entry instead.
    root = _root(tmp_path)
    registry = _registry()
    registry["shared_paths"].append({
        "glob": "pyproject.toml",                      # duplicate of the existing entry
        "category": "package-metadata",
        "requires": "shared-lease-or-lead-approval",
        "default_approvers": {"agents": ["beta"]},     # DIFFERENT approver
    })
    with pytest.raises(dom.DomainError, match="duplicate"):
        dom.validate_registry(registry, Store(root).load_config())


def test_normalize_repo_path_rejects_absolute_and_escape() -> None:
    assert dom.normalize_repo_path(r".\\src\\agenttalk\\..\\agenttalk\\cli.py") == "src/agenttalk/cli.py"
    assert dom.normalize_repo_path("SRC/CLI.PY", casefold=True) == "src/cli.py"

    with pytest.raises(dom.DomainError, match="escapes"):
        dom.normalize_repo_path("../outside.py")
    with pytest.raises(dom.DomainError, match="repo-relative"):
        dom.normalize_repo_path("C:/tmp/outside.py")
    with pytest.raises(dom.DomainError, match="repo-relative"):
        dom.normalize_repo_path("C:tmp/outside.py")
    with pytest.raises(dom.DomainError, match="repo-relative"):
        dom.normalize_repo_path("/absolute/outside.py")


def test_check_path_reports_owned_overlap_shared_and_unowned(tmp_path: Path) -> None:
    root = _root(tmp_path)
    registry = dom.validate_registry(_registry(), Store(root).load_config())

    cli_path = dom.check_path(registry, r"src\\agenttalk\\cli.py", casefold_paths=False)
    shared_path = dom.check_path(registry, "pyproject.toml", casefold_paths=False)
    unowned_path = dom.check_path(registry, "README.md", casefold_paths=False)

    assert cli_path["domains"] == ["cli", "core"]
    assert cli_path["overlap"] is True
    assert shared_path["shared_paths"] == [{
        "glob": "pyproject.toml",
        "category": "package-metadata",
        "requires": "shared-lease-or-lead-approval",
    }]
    assert shared_path["unowned"] is True
    assert unowned_path["domains"] == []
    assert unowned_path["unowned"] is True


def test_check_path_can_casefold_globs(tmp_path: Path) -> None:
    root = _root(tmp_path)
    registry = _registry()
    registry["domains"]["docs"] = {
        "title": "Docs",
        "owners": {"agents": ["alpha"]},
        "reviewers": {},
        "curators": {},
        "owned_globs": ["README.md"],
    }
    data = dom.validate_registry(registry, Store(root).load_config())

    sensitive = dom.check_path(data, "readme.MD", casefold_paths=False)
    insensitive = dom.check_path(data, "readme.MD", casefold_paths=True)

    assert "docs" not in sensitive["domains"]
    assert "docs" in insensitive["domains"]


@pytest.mark.parametrize("descriptor", [
    "src/**/*.py",
    "src/file?.py",
    "src/[unterminated.py",
])
def test_check_path_rejects_glob_descriptors(
    tmp_path: Path,
    descriptor: str,
) -> None:
    root = _root(tmp_path)
    registry = dom.validate_registry(_registry(), Store(root).load_config())

    with pytest.raises(
        dom.DomainError,
        match="check_path needs a concrete repo path",
    ) as exc_info:
        dom.check_path(registry, descriptor, casefold_paths=False)

    assert descriptor in str(exc_info.value)


def test_domain_cli_validate_list_show_and_check_path_json(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    root = _root(tmp_path)
    _write_registry(root, _registry())

    assert _run(["domain", "validate", "--json"], root) == 0
    validate_payload = json.loads(capsys.readouterr().out)
    assert validate_payload["valid"] is True
    assert validate_payload["domain_count"] == 2
    assert validate_payload["shared_path_count"] == 1

    assert _run(["domain", "list", "--json"], root) == 0
    list_payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in list_payload["domains"]] == ["cli", "core"]

    assert _run(["domain", "--json", "list"], root) == 0
    parent_json_list_payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in parent_json_list_payload["domains"]] == ["cli", "core"]

    assert _run(["domain", "show", "cli", "--json"], root) == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["resolved"]["owners"] == ["alpha", "gamma"]
    assert show_payload["resolved"]["reviewers"] == ["beta"]

    assert _run(["domain", "check-path", "src/agenttalk/cli.py", "pyproject.toml", "--json"], root) == 0
    check_payload = json.loads(capsys.readouterr().out)
    assert check_payload["paths"][0]["overlap"] is True
    assert check_payload["paths"][1]["shared"] is True


def test_domain_cli_check_path_rejects_glob_descriptor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    root = _root(tmp_path)
    _write_registry(root, _registry())

    assert _run(["domain", "check-path", "src/**/*.py"], root) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "src/**/*.py" in captured.err
    assert "check_path needs a concrete repo path" in captured.err


def test_domain_cli_missing_registry_is_valid_empty(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _root(tmp_path)

    assert _run(["domain", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_exists"] is False
    assert payload["domains"] == []


def test_load_registry_tolerates_utf8_bom(tmp_path: Path) -> None:
    """domains.json is authored BY HAND (README: 'author domains.json by hand'); a
    Notepad/PowerShell save can prepend a UTF-8 BOM. The loader must decode it
    BOM-tolerantly, not raise DomainError citing 'Unexpected UTF-8 BOM' (v0.75.3, D-26)."""
    root = _root(tmp_path)
    cfg = Store(root).load_config()
    p = root / ".agenttalk" / dom.FILENAME
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(_registry(), indent=2).encode("utf-8"))
    reg = dom.load_registry(p, cfg)          # must NOT raise on the leading BOM
    assert "cli" in reg.data["domains"]
