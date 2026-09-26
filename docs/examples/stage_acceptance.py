"""Create inert Java staging examples; no downloads or tool execution.

Run with Python and an absolute destination under your task scratch directory.
The generated distributions are synthetic bytes, not executable Java software.
"""

import hashlib
import json
from pathlib import Path
import sys


def stage(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    def write(name, value):
        data = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode("utf-8")
        (root / name).write_bytes(data)
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    provenance = {"source": "vendor:synthetic-java", "retrieved_at": "2026-01-01T00:00:00Z",
                  "checksum_source": "vendor:synthetic-checksums", "independent_verification": False,
                  "record": "provenance", "verification": None}
    files = []
    for fid, name, role, version in [
        ("jdk", "jdk-21.zip", "distribution", "21.0.1"),
        ("checker", "checker-1.jar", "distribution", "1.0"),
        ("advisories", "advisories.dat", "distribution", "2026.01"),
        ("provenance", "provenance.json", "provenance", None),
        ("adapter", "adapter.json", "adapter", None),
        ("config", "checker-config.json", "config", None),
    ]:
        files.append({"id": fid, "role": role, **write(name, {"synthetic": fid}), "version": version,
                      "expires_at": None, "provenance": provenance if role == "distribution" else None})
    distribution = {"id": "advisories", "sha256": files[2]["sha256"]}
    files.append({"id": "snapshot", "role": "snapshot", "version": "2026.01",
                  **write("snapshot.json", {"distribution": distribution}), "distribution": distribution,
                  "expires_at": "2027-01-01T00:00:00Z", "provenance": provenance})
    failure = {"unavailable": "not-run", "mismatch": "fail", "expired": "not-run",
               "absent_proof": "not-run", "attempted_fetch": "fail"}
    offline = {"mode": "external-denial", "positive_control": "DENIAL_OK",
               "cache_hit": None, "real_fetch": "FETCH"}
    entries = []
    for eid, kind, artifact, version, banner in [
        ("java", "toolchain", "jdk", "21.0.1", "java 21.0.1"),
        ("lint", "checker", "checker", "1.0", None),
    ]:
        entries.append({"id": eid, "kind": kind, "artifact": artifact, "version": version,
                        "expected_banner": banner, "dependencies": [] if eid == "java" else ["java"],
                        "inputs": [] if eid == "java" else ["advisories"],
                        "snapshots": [] if eid == "java" else ["snapshot"], "provenance": provenance,
                        "command": {"argv": ["{artifact}"], "cwd": "{checkout}", "inputs": [], "outputs": []},
                        "offline": offline, "failure_policy": failure,
                        "measurement": {"comparator": "adapter", "parser": "adapter", "config": "config",
                                        "normalizer": "adapter"} if kind == "checker" else None})
    registry = {"schema_version": 2, "files": files, "entries": entries}
    registry_ref = write("registry.json", registry)
    environment = {"schema_version": 1, "runtime": ["java"], "compiler": [], "package_manager": [], "services": [],
                   "os": "synthetic", "locale": "C", "timezone": "UTC", "environment_digest": "a" * 64,
                   "config_digest": "b" * 64, "scratch": "isolated", "cache_overlay": "fresh-writable",
                   "service_data": "fresh", "time_limit_seconds": 60, "memory_limit_bytes": 1024,
                   "row_overrides": []}
    plan = {"schema_version": 4, "plan_id": "java-example", "project_id": "synthetic-project", "scope": "milestone",
            "authors": ["author"], "partitions": [{"id": "checks", "agents": ["runner"]}],
            "rows": [{"id": "lint", "partition": "checks", "policy": "gating", "comparator": "exit-code",
                      "expected": 0, "artifact": "raw", "field": "exit_code", "registry_entries": ["java", "lint"]}],
            "registry_ref": "registry.json", "registry_digest": registry_ref["sha256"], "trust_profile": "cooperative",
            "cold_policy": {"reviewer": "cold", "roster": [{"actor": "cold", "vendor": "vendor"}],
                            "absence_disclosure": "synthetic example", "change_base": "c" * 40},
            "environment": environment}
    write("plan.json", plan)
    observed = []
    for entry in entries:
        observed.append({"id": entry["id"], "version": entry["version"],
                         "banner": write(entry["id"] + ".txt", b"Runtime information\njava 21.0.1\n"),
                         "offline": {"mode": "external-denial", "egress_denied": True, "positive_control": True,
                                     "cache_hit": False, "attempted_fetch": False, "endpoints": [],
                                     "log": write(entry["id"] + ".log", b"DENIAL_OK\n")}})
    write("observation.json", {"schema_version": 1, "registry_hash": registry_ref["sha256"],
                               "observed_at": "2026-01-02T00:00:00Z", "environment": environment,
                               "entries": observed})


if __name__ == "__main__":
    stage(sys.argv[1])
