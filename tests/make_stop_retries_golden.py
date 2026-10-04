"""Make tests/golden/stop_retries_off_fc791ece.json from the code on PYTHONPATH.

Run it ONLY against agenttalk master fc791ece (the code before the usage-limit park):

    git archive fc791ece src | tar -x -C <scratch>/master
    PYTHONPATH=<scratch>/master/src python tests/make_stop_retries_golden.py

See tests/golden_stop_retries_scenarios.py for what is recorded.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import golden_stop_retries_scenarios as scenarios  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "stop_retries_off_fc791ece.json"

if __name__ == "__main__":
    import agenttalk

    if Path(agenttalk.__file__).with_name("wrapper").joinpath("usage_park.py").exists():
        raise SystemExit("this is not master fc791ece: the golden file records the code before the switch")
    with tempfile.TemporaryDirectory() as tmp:
        record = scenarios.capture_all(Path(tmp))
    GOLDEN.parent.mkdir(exist_ok=True)
    # newline="" keeps "\n" as it is: the file must be byte-identical on every platform
    with GOLDEN.open("w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(record, indent=1, sort_keys=True) + "\n")
    print("wrote", GOLDEN)
