"""Non-voting Windows source/wheel trial. Full dev-gate evidence remains authoritative."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
# The collector invokes only gh with argument lists, never a shell.
import subprocess  # nosec B404
import time
from datetime import datetime
from pathlib import Path

from agenttalk import dev_gate as gate

TYPE = "agenttalk-windows-mode-trial-v1"
MODES = ("source", "wheel")


def require(condition, message):
    if not condition:
        raise gate.GateBlock("trial_blocked", message)


def partition(manifest, leg, mode):
    """Bindings run twice. Every other Windows check belongs to exactly one mode."""
    require(mode in MODES and leg.startswith("windows/"), "Windows source/wheel modes only")
    plan = gate.required_check_ids(manifest, execution_scope="ci-leg", ci_leg=leg)
    return [item for item in plan if item in {"git-binding", "final-binding"}
            or (item.startswith("pytest-source-") == (mode == "source"))]


def identity(root):
    binding = gate.capture_candidate_binding(root)
    require(binding.clean, "candidate must be clean")
    return {"candidate_sha": binding.candidate_sha, "candidate_tree": binding.candidate_tree,
            "manifest_sha256": binding.manifest_sha256,
            "repository": os.environ["GITHUB_REPOSITORY"], "run_id": os.environ["GITHUB_RUN_ID"],
            "attempt": os.environ["GITHUB_RUN_ATTEMPT"], "workflow": os.environ["GITHUB_WORKFLOW_REF"]}


def artifact_name(context, leg, mode):
    return f"windows-trial-{context['run_id']}-{context['attempt']}-{leg.replace('/', '-')}-{mode}"


def job_name(leg, mode):
    return f"Windows mode trial ({leg}/{mode})"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_trial(root, output, leg, mode):
    context = identity(root)
    installs = {}
    result = gate.execute_gate(root=root, ci_leg=leg, evidence_path=output / "partial.json",
                               trial_mode=mode, trial_installs=installs)
    manifest = gate.load_bound_manifest(gate.capture_candidate_binding(root))
    record = result.artifact
    assigned = partition(manifest, leg, mode)
    # A mode's ordinary gate artifact MUST remain BLOCK: it lacks the other mode.
    require(record["verdict"] == "block" and record["complete"] is False, "a mode claimed a complete gate")
    for item in record["artifacts"].values():
        target = output / "packages" / item["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item["path"], target)
    for proof in installs.values():
        target = output / "wheel-test-install.log"
        shutil.copyfile(proof["log"]["path"], target)
        proof["log"]["artifact_path"] = target.name
    files = {p.relative_to(output).as_posix(): gate._sha256_file(p)
             for p in output.rglob("*") if p.is_file()}
    complete = all(c["status"] == "pass" for c in record["checks"] if c["id"] in assigned)
    envelope = {"artifact_type": TYPE, "authoritative": False, "status": "complete" if complete else "block",
                "context": context, "leg": leg, "mode": mode, "assigned": assigned,
                "files": files, "wheel_test_installs": installs}
    write_json(output / "trial.json", envelope)
    return 0 if complete else 1


def validate_part(directory, context, manifest, leg, mode):
    envelope = json.loads((directory / "trial.json").read_bytes())
    require(envelope["artifact_type"] == TYPE and envelope["authoritative"] is False
            and envelope["status"] == "complete", "missing, blocked or voting trial result")
    require(envelope["context"] == context and envelope["leg"] == leg and envelope["mode"] == mode,
            "mixed candidate, manifest, run, attempt, workflow, leg or mode")
    actual = {p.relative_to(directory).as_posix(): gate._sha256_file(p)
              for p in directory.rglob("*") if p.is_file() and p != directory / "trial.json"}
    require(actual == envelope["files"] and "partial.json" in actual, "missing or corrupted bundle file")
    record = gate.validate_run_artifact(json.loads((directory / "partial.json").read_bytes()),
                                      manifest, bundle_root=directory)
    assigned = partition(manifest, leg, mode)
    require(envelope["assigned"] == assigned and record["ci_leg"] == leg, "partition differs from manifest")
    require(record["verdict"] == "block" and record["complete"] is False, "partial mode claimed pass")
    subject = record["subject"]
    require(all(subject[k] == context[k] for k in ("candidate_sha", "candidate_tree"))
            and record["manifest"]["sha256"] == context["manifest_sha256"], "inner binding differs")
    require(all(subject[k] is True for k in ("clean_before", "clean_after", "head_stable")), "unstable candidate")
    require(all((c["status"] == "pass") == (c["id"] in assigned) for c in record["checks"]),
            "missing assigned check or execution outside the partition")
    require(all("artifact_path" in c["log"] for c in record["checks"]), "uncollected proof logs")
    for item in record["artifacts"].values():
        data = (directory / "packages" / item["filename"]).read_bytes()
        require(gate.sha256_bytes(data) == item["sha256"] and len(data) == item["size_bytes"],
                "package bytes differ from build proof")
    if mode == "wheel":
        minor = leg.split("/")[1]
        proof = envelope["wheel_test_installs"]
        require(set(proof) == {minor}, "missing or duplicate installed-test wheel link")
        proof = proof[minor]
        test = next(c for c in record["checks"] if c["id"].startswith("pytest-wheel-"))
        wheel = record["artifacts"]["wheel"]
        expected = gate.isolated_tool_argv(test["runtime_environment"]["python_path"], "pip", "install",
                                          "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                                          "--index-url", manifest["checks"]["wheel-contract"]["dependency_index"],
                                          wheel["path"])
        require(type(proof["exit_code"]) is int and proof["exit_code"] == 0
                and proof["argv"] == expected and proof["python_path"] == test["tool"]["path"]
                and proof["wheel_sha256"] == wheel["sha256"]
                and proof["log"]["artifact_path"] == "wheel-test-install.log"
                and actual["wheel-test-install.log"] == proof["log"]["sha256"], "tested wheel link differs")
    else:
        require(not record["artifacts"] and not envelope["wheel_test_installs"], "source mode has package proofs")
    return record


def select_inputs(artifacts, jobs, context, manifest):
    """Never reuse successful jobs from an earlier attempt: rerun ALL trial modes."""
    pairs = [(leg, mode) for leg in gate.expected_ci_legs(manifest) if leg.startswith("windows/") for mode in MODES]
    expected = {artifact_name(context, leg, mode) for leg, mode in pairs}
    prefix = f"windows-trial-{context['run_id']}-{context['attempt']}-"
    selected = [a for a in artifacts if a["name"].startswith(prefix)]
    require(len(selected) == len(expected) and {a["name"] for a in selected} == expected,
            "missing, duplicate or unexpected mode artifacts; rerun all trial modes in one attempt")
    require(len({a["id"] for a in selected}) == len(selected), "duplicate artifact IDs")
    results = []
    for leg, mode in pairs:
        matches = [j for j in jobs if j["name"] == job_name(leg, mode)]
        require(len(matches) == 1, "missing or duplicate current-attempt job")
        job = matches[0]
        require(str(job["run_id"]) == context["run_id"] and str(job["run_attempt"]) == context["attempt"]
                and job["status"] == "completed" and job["conclusion"] == "success", "mode job did not succeed")
        artifact = next(a for a in selected if a["name"] == artifact_name(context, leg, mode))
        require(not artifact["expired"] and str(artifact["workflow_run"]["id"]) == context["run_id"]
                and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")), "unbound artifact digest")
        results.append((leg, mode, artifact, job))
    return results


def gh(endpoint, *, pages=False, destination=None):
    argv = ["gh", "api", endpoint]
    if pages:
        argv += ["--paginate", "--slurp"]
    if destination is not None:
        with destination.open("wb") as sink:
            subprocess.run(argv, stdout=sink, check=True, timeout=180)  # nosec B603
        return None
    return json.loads(subprocess.run(argv, capture_output=True, check=True, timeout=180).stdout)  # nosec B603


def elapsed(start, end):
    return (datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def aggregate(root, output):
    began = time.monotonic()
    context = identity(root)
    manifest = gate.load_bound_manifest(gate.capture_candidate_binding(root))
    api = f"repos/{context['repository']}/actions"
    run_api = f"{api}/runs/{context['run_id']}"
    run = gh(f"{run_api}/attempts/{context['attempt']}")
    require(str(run["id"]) == context["run_id"] and str(run["run_attempt"]) == context["attempt"], "wrong attempt")
    jobs = [j for page in gh(f"{run_api}/attempts/{context['attempt']}/jobs?per_page=100", pages=True)
            for j in page["jobs"]]
    artifacts = [a for page in gh(f"{run_api}/artifacts?per_page=100", pages=True) for a in page["artifacts"]]
    write_json(output / "api-evidence.json", {"run": run, "jobs": jobs, "artifacts": artifacts})
    rows = []
    for leg, mode, artifact, job in select_inputs(artifacts, jobs, context, manifest):
        archive = output / f"{artifact['id']}.zip"
        gh(f"{api}/artifacts/{artifact['id']}/zip", destination=archive)
        require("sha256:" + gate._sha256_file(archive) == artifact["digest"], "download digest mismatch")
        directory = output / str(artifact["id"])
        gate._safe_extract_zip(archive, directory)
        record = validate_part(directory, context, manifest, leg, mode)
        suite = sum(c["duration_ms"] for c in record["checks"] if c["kind"] == "pytest") / 1000
        gate_step = next(s for s in job["steps"] if s["name"] == "Run trial mode")
        rows.append({"leg": leg, "mode": mode, "artifact_id": artifact["id"], "digest": artifact["digest"],
                     "job_id": job["id"], "queue_s": elapsed(run["run_started_at"], job["started_at"]),
                     "setup_s": elapsed(job["started_at"], gate_step["started_at"]), "suite_s": suite,
                     "job_s": elapsed(job["started_at"], job["completed_at"]), "finished_at": job["completed_at"]})
    baseline = [j for j in jobs if j["name"].startswith("dev-gate (windows/")]
    result = {"artifact_type": TYPE + "-aggregate", "authoritative": False, "verdict": "trial-complete",
              "context": context, "parts": rows, "baseline_jobs": baseline,
              "aggregate_s": time.monotonic() - began,
              "note": "Trial only; the existing dev-gate aggregate remains required. No cutover authorised."}
    write_json(output / "trial-aggregate.json", result)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "aggregate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--leg")
    parser.add_argument("--mode", choices=MODES)
    args = parser.parse_args(argv)
    root = gate.discover_repo_root()
    output = args.output.resolve()
    gate._ensure_external(output, root, None, "trial evidence")
    try:
        # Do not overwrite or merge another invocation's bundle on retry.
        output.mkdir(parents=True, exist_ok=False)
        if args.action == "run":
            require(args.leg is not None and args.mode is not None, "run requires --leg and --mode")
            return run_trial(root, output, args.leg, args.mode)
        return aggregate(root, output)
    except (gate.GateBlock, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Windows trial BLOCK: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
