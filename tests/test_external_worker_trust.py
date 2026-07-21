from __future__ import annotations

import json

import pytest

from agenttalk import cli
from agenttalk.store import Store


def make_store(tmp_path) -> Store:
    store = Store(tmp_path)
    store.init(["lead", "worker", "reviewer"])
    store.set_role("lead", "lead")
    return store


def write_signoff_policy(store: Store, reviewers: dict) -> None:
    (store.dir / "signoffs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults": {"reviewers": reviewers},
                "risk_policies": {},
            }
        ),
        encoding="utf-8",
    )


def test_external_worker_metadata_is_opt_in_and_load_validated(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_trust_class("worker", "external-worker")
    assert store.trust_class("worker") == "external-worker"
    assert store.trust_class("reviewer") is None

    cfg = store.load_config()
    cfg["trust_classes"]["worker"] = "unknown"
    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="trust class"):
        store.load_config()


def test_roster_cli_add_and_show_external_worker_metadata(tmp_path, capsys) -> None:
    store = make_store(tmp_path)
    assert cli.main([
        "--root",
        str(tmp_path),
        "roster",
        "add",
        "qwen-dev-1",
        "--trust-class",
        "external-worker",
    ]) == 0
    capsys.readouterr()

    assert cli.main(["--root", str(tmp_path), "roster", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trust_classes"]["qwen-dev-1"] == "external-worker"
    assert store.trust_class("qwen-dev-1") == "external-worker"


def test_external_worker_cannot_be_added_or_mutated_to_lead(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="cannot hold the lead role"):
        store.add_agent("qwen", role="lead", trust_class="external-worker")
    assert "qwen" not in store.active_agents()

    store.set_trust_class("worker", "external-worker")
    with pytest.raises(ValueError, match="cannot hold the lead role"):
        store.set_role("worker", "lead")
    assert store.roles().get("worker") != "lead"


def test_external_worker_cannot_be_operator_facing(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_trust_class("worker", "external-worker")
    with pytest.raises(ValueError, match="cannot be operator-facing"):
        store.set_operator_facing("worker")
    assert store.operator_facing() is None


@pytest.mark.parametrize(
    "reviewers",
    [
        {"agents": ["worker"]},
        {"roles": ["reviewer"]},
        {"groups": ["gating-reviewers"]},
    ],
)
def test_external_worker_cannot_become_signoff_candidate(tmp_path, reviewers) -> None:
    store = make_store(tmp_path)
    write_signoff_policy(store, reviewers)
    if reviewers.get("roles"):
        store.set_role("worker", "reviewer")
    if reviewers.get("groups"):
        store.set_group("gating-reviewers", ["worker"])
    with pytest.raises(ValueError, match="cannot be a signoff candidate"):
        store.set_trust_class("worker", "external-worker")
    assert store.trust_class("worker") is None


def test_group_mutation_cannot_make_running_external_worker_countable(tmp_path) -> None:
    store = make_store(tmp_path)
    write_signoff_policy(store, {"groups": ["gating-reviewers"]})
    store.set_trust_class("worker", "external-worker")
    with pytest.raises(ValueError, match="cannot be a signoff candidate"):
        store.set_group("gating-reviewers", ["worker", "reviewer"])
    assert "gating-reviewers" not in store.groups()


def test_role_mutation_cannot_make_running_external_worker_countable(tmp_path) -> None:
    store = make_store(tmp_path)
    write_signoff_policy(store, {"roles": ["reviewer"]})
    store.set_trust_class("worker", "external-worker")

    with pytest.raises(ValueError, match="cannot be a signoff candidate"):
        store.set_role("worker", "reviewer")

    assert store.roles().get("worker") != "reviewer"


def test_add_agent_group_path_uses_same_candidate_guard(tmp_path) -> None:
    store = make_store(tmp_path)
    write_signoff_policy(store, {"groups": ["gating-reviewers"]})
    with pytest.raises(ValueError, match="cannot be a signoff candidate"):
        store.add_agent(
            "qwen",
            groups=["gating-reviewers"],
            trust_class="external-worker",
        )
    assert "qwen" not in store.active_agents()


def test_rename_carries_external_worker_metadata_and_remove_tombstones_it(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_trust_class("worker", "external-worker")
    store.rename_agent("worker", "qwen-worker")
    assert store.trust_class("qwen-worker") == "external-worker"
    assert "worker" not in store.trust_classes()
    store.remove_agent("qwen-worker")
    assert "qwen-worker" not in store.trust_classes()
    assert "qwen-worker" in store.retired_agents()
    with pytest.raises(ValueError, match="retired tombstone"):
        store.add_agent("qwen-worker", role="reviewer")


def test_external_worker_trust_is_sticky_and_survives_force_init(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_trust_class("worker", "external-worker")

    with pytest.raises(ValueError, match="cannot be cleared"):
        store.set_trust_class("worker", None)

    store.init(["lead", "worker", "reviewer"], force=True)
    assert store.trust_class("worker") == "external-worker"

    store.init(["lead", "reviewer"], force=True)
    assert "worker" in store.retired_agents()
    with pytest.raises(ValueError, match="retired tombstone"):
        store.add_agent("worker")


def test_force_init_recovers_external_trust_from_a_partly_malformed_config(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    store.set_trust_class("worker", "external-worker")
    raw = json.loads(store.config_path.read_text(encoding="utf-8"))
    raw["agents"] = "malformed-but-trust-record-is-recoverable"
    store.config_path.write_text(json.dumps(raw), encoding="utf-8")

    store.init(["lead", "worker", "reviewer"], force=True)

    assert store.trust_class("worker") == "external-worker"


def test_domain_reviewer_cannot_be_reclassified_as_external_worker(tmp_path) -> None:
    store = make_store(tmp_path)
    (store.dir / "signoffs.json").write_text(
        json.dumps({
            "schema_version": 1,
            "defaults": {"reviewers": {}},
            "risk_policies": {
                "security": [{
                    "id": "domain-security",
                    "required_count": 1,
                    "candidates": {},
                    "include_domain_reviewers": True,
                }],
            },
        }),
        encoding="utf-8",
    )
    (store.dir / "domains.json").write_text(
        json.dumps({
            "schema_version": 1,
            "domains": {
                "auth": {
                    "title": "Auth",
                    "owners": {"agents": ["lead"]},
                    "reviewers": {"agents": ["worker"]},
                    "owned_globs": ["src/auth/**"],
                },
            },
            "shared_paths": [],
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot be a signoff candidate"):
        store.set_trust_class("worker", "external-worker")

    assert store.trust_class("worker") is None


def test_domain_referenced_rename_cannot_make_external_worker_countable(tmp_path) -> None:
    store = make_store(tmp_path)
    (store.dir / "signoffs.json").write_text(
        json.dumps({
            "schema_version": 1,
            "defaults": {"reviewers": {}},
            "risk_policies": {
                "security": [{
                    "id": "domain-security",
                    "required_count": 1,
                    "candidates": {},
                    "include_domain_reviewers": True,
                }],
            },
        }),
        encoding="utf-8",
    )
    (store.dir / "domains.json").write_text(
        json.dumps({
            "schema_version": 1,
            "domains": {
                "auth": {
                    "title": "Auth",
                    "owners": {"agents": ["lead"]},
                    "reviewers": {"agents": ["qwen-worker"]},
                    "owned_globs": ["src/auth/**"],
                },
            },
            "shared_paths": [],
        }),
        encoding="utf-8",
    )
    store.set_trust_class("worker", "external-worker")

    with pytest.raises(ValueError, match="cannot be a signoff candidate"):
        store.rename_agent("worker", "qwen-worker")

    assert "worker" in store.active_agents()
    assert "qwen-worker" not in store.active_agents()
    assert store.trust_class("worker") == "external-worker"
