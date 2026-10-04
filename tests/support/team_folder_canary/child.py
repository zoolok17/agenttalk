"""Team-folder canary child: probe this process, then (optionally) run one stub turn.

Started by the wrapper's real spawn path in place of a model CLI, or with the
gateway-backed child environment. The canary's own settings travel in
AGENTTALK_CANARY_* variables, which both the ordinary and the gateway-backed child
environments keep (the gateway-backed one keeps every AGENTTALK_ variable).
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import probe  # noqa: E402 - found through the line above

# A child whose environment keeps no AGENTTALK_ variable (the comprehension worker's)
# gets the same settings on its command line: --canary <out> <work> <label> <write>.
if sys.argv[1:2] == ["--canary"]:
    out, work, label, write = sys.argv[2:6]
    del sys.argv[1:6]
else:
    out = os.environ["AGENTTALK_CANARY_OUT"]
    work = os.environ["AGENTTALK_CANARY_WORK"]
    label = os.environ.get("AGENTTALK_CANARY_LABEL", "child")
    write = os.environ.get("AGENTTALK_CANARY_WRITE", "0")

result = probe.observe(label, work, write == "1")
Path(out).write_text(json.dumps(result, indent=2), encoding="utf-8")

stub = os.environ.get("AGENTTALK_CANARY_STUB")
if stub:
    sys.argv = [stub, *sys.argv[1:]]
    runpy.run_path(stub, run_name="__main__")
