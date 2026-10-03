"""Make tests/golden/off_loop_2112cec.json from the code on PYTHONPATH.

Run it ONLY against agenttalk master 2112cec (the code before the turn journal):

    git archive 2112cec src | tar -x -C <scratch>/master
    PYTHONPATH=<scratch>/master/src python tests/make_off_golden.py

See tests/golden_off_scenarios.py for what is recorded.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import golden_off_scenarios as scenarios  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "off_loop_2112cec.json"

if __name__ == "__main__":
    import agenttalk

    if "turn_events" in sys.modules or Path(agenttalk.__file__).with_name("turn_events.py").exists():
        raise SystemExit("this is not master 2112cec: the golden file records the code before the journal")
    with tempfile.TemporaryDirectory() as tmp:
        record = scenarios.capture_all(Path(tmp))
    GOLDEN.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote", GOLDEN)
