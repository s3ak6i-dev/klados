#!/usr/bin/env python3
"""Build a richer real timeline via the SDK and export it as JSON for the Timeline UI:
genesis -> snapshot -> fork 3 strategies -> one branch snapshots again -> fork 2."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from klados import Sandbox

sb = Sandbox.create(image="klados/agent-base:py3.12")
s1 = sb.snapshot(label="task loaded")
kids = s1.fork(3)                       # try three strategies
s2 = kids[1].snapshot(label="branch 1 - fix applied")
gk = s2.fork(2)                         # explore two sub-branches from the winner

tl = sb.timeline()
out = os.path.join(os.path.dirname(__file__), "..", "engine", "timeline_export.json")
with open(out, "w") as f:
    json.dump(tl._d, f, indent=2)
print(f"wrote {out}: {len(tl.snapshots)} snapshots, {len(tl.instances)} instances")

for c in kids + gk:
    try:
        c.destroy()
    except Exception:
        pass
sb.destroy()
