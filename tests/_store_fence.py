"""The test suite's store fence: no test may open a message store outside pytest's
temporary folder, not even through a subprocess it starts.

``AGENTTALK_STORE_FENCE`` makes agenttalk refuse every store outside that folder, so a
test run from a checkout that holds a live ``.agenttalk`` can never reach it, however
the root is found (``--root``, ``AGENTTALK_ROOT`` or the walk up from the current
folder). Each refusal is also written to ``AGENTTALK_STORE_FENCE_REPORT``, and a test
that caused one fails, even when it expected a non-zero exit. Both settings are
inherited by subprocesses. A test that probes the fence on purpose points the report
at a file of its own.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FENCE_ENV = "AGENTTALK_STORE_FENCE"
REPORT_ENV = "AGENTTALK_STORE_FENCE_REPORT"


def _report_size(report: Path) -> int:
    # os and open(), not Path methods: this runs while a test's own patches are still in place
    try:
        return os.path.getsize(report)
    except OSError:
        return 0


def _report_since(report: Path, offset: int) -> list[str]:
    with open(report, "rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", "replace").splitlines()


@pytest.fixture(scope="session", autouse=True)
def _store_fence(tmp_path_factory: pytest.TempPathFactory):
    basetemp = tmp_path_factory.getbasetemp().resolve()
    report = basetemp / "store-fence-refusals.txt"
    saved = {name: os.environ.get(name) for name in (FENCE_ENV, REPORT_ENV)}
    os.environ[FENCE_ENV] = str(basetemp)
    os.environ[REPORT_ENV] = str(report)
    yield report
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _no_store_outside_the_test_folder(_store_fence: Path):
    before = _report_size(_store_fence)
    yield
    if _report_size(_store_fence) > before:
        reached = _report_since(_store_fence, before)
        pytest.fail(
            "this test reached a message store outside pytest's temporary folder: "
            + ", ".join(reached),
            pytrace=False,
        )
