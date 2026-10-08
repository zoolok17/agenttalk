"""Select the lighter PR checks only for an entirely prose-only change."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath


def docs_only(paths: list[str]) -> bool:
    def prose(path: str) -> bool:
        parts = PurePosixPath(path).parts
        if (not parts or any(p in {"..", "skills", "SKILL.md"} for p in parts)
                or "\n" in path or "\r" in path):
            return False
        return path in {"README.md", "CHANGELOG.md", "SECURITY.md"} or (
            parts[0] == "docs" and path.endswith(".md")
        )

    return bool(paths) and all(prose(path) for path in paths)


def pr_is_docs_only(event: dict, repo: Path) -> bool:
    pr = event["pull_request"]
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("The PR base and head must be full commit IDs")
    # No API file-count limit; no rename detection hiding a deleted code path.
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", "--"],
        cwd=repo, timeout=60,
    ).decode("utf-8")
    return docs_only(changed.rstrip("\0").split("\0")) if changed else False


if __name__ == "__main__":
    selected = False
    if os.environ["GITHUB_EVENT_NAME"] == "pull_request":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        selected = pr_is_docs_only(event, Path.cwd())
    # Errors above fail the scope job; they must never produce a docs-only pass.
    print(f"docs_only={str(selected).lower()}")
