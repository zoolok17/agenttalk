"""An explicit readiness barrier for two-process tests (lead loop, slice 1).

Each worker reports "ready" (a file) after its imports and setup and then waits for "go" (another file); the parent
releases them only when every worker has reported. No clock deadline decides the overlap: a slow start delays the
release instead of letting one worker finish alone."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def barrier_code(barrier_dir: Path, tag: str) -> str:
    """Source to paste right before the contested operation in a worker script."""
    return (
        "import time as _t\n"
        "from pathlib import Path as _P\n"
        f"_P({str(barrier_dir / ('ready-' + tag))!r}).write_text('1')\n"
        f"while not _P({str(barrier_dir / 'go')!r}).exists():\n"
        "    _t.sleep(0.002)\n"
    )


def run_released_together(barrier_dir: Path, scripts: list[str], *,
                          timeout: float = 60.0) -> list[tuple[int, str, str]]:
    """Start one process per script, wait until all are ready, release them together, return (rc, out, err)."""
    barrier_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for code in scripts]
    deadline = time.monotonic() + timeout
    try:
        while len(list(barrier_dir.glob("ready-*"))) < len(procs):
            if time.monotonic() > deadline or any(p.poll() is not None for p in procs):
                raise AssertionError("a worker did not reach the barrier: %r" % [p.poll() for p in procs])
            time.sleep(0.005)
        (barrier_dir / "go").write_text("1")
        outs = [p.communicate(timeout=timeout) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    return [(p.returncode, o, e) for p, (o, e) in zip(procs, outs, strict=True)]
