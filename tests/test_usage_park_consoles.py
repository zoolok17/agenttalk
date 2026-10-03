"""Runs the Node tests for a seat parked on a usage limit in both consoles (skipped without Node)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("script", ["console2_usage_park.test.mjs", "console_usage_park.test.mjs"])
def test_usage_park_console_node_tests(script: str) -> None:
    result = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / script)], capture_output=True, text=True, encoding="utf-8",
        timeout=120, cwd=REPO_ROOT, env={**os.environ, "TZ": "UTC"})
    assert result.returncode == 0, result.stdout + result.stderr
    last = result.stdout.strip().splitlines()[-1]
    passed, total = re.search(r"(\d+)/(\d+) passed", last).groups()
    assert passed == total and int(total) > 0, last


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("name", ["console.js", "console2-model.js"])
def test_the_edited_console_files_pass_node_check(name: str) -> None:
    result = subprocess.run(["node", "--check", str(REPO_ROOT / "src" / "agenttalk" / "web_static" / name)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
