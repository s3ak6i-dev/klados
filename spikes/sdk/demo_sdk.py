#!/usr/bin/env python3
"""SDK demo — the PRD north-star flow through the klados Python SDK (talks to kladosd)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))  # find the local `klados` package
from klados import Sandbox

print("Sandbox.create(...)")
sb = Sandbox.create(image="klados/agent-base:py3.12")
print("  ", sb)

snap = sb.snapshot(label="before-risky-refactor")
print("sb.snapshot():", snap)

children = snap.fork(4)
print(f"snap.fork(4) -> {len(children)} children:")
for c in children:
    print("   ", c)

tl = sb.timeline()
print("sb.timeline():", tl)

print("cleanup…")
for c in children:
    c.destroy()
sb.destroy()
print("\nOK — SDK create / snapshot / fork / timeline / destroy all work end to end")
