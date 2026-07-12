#!/usr/bin/env python3
"""Spike E — clock staleness on restore (tests R8).

Firecracker does not inject wall time on restore, so a resumed guest's clock is behind
true time by the pause duration. This measures the skew directly: boot a guest streaming
its wall clock, snapshot, pause for --pause-s of real time, restore, and compare the
guest's post-resume wall clock to host true time.

The fix (guest agent, on the resume/FORKED event): step CLOCK_REALTIME to host-provided
true time and emit `time-jumped` (PRD §6). This quantifies WHY that fixup is needed.
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile
import time

import fc

TIME_RE = re.compile(rb"TIME wall=([0-9.]+) mono=([0-9.]+)")


def parse(path):
    try:
        data = open(path, "rb").read()
    except FileNotFoundError:
        return []
    return [(float(m.group(1)), float(m.group(2))) for m in TIME_RE.finditer(data)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"))
    ap.add_argument("--rootfs", default="/opt/klados/assets/rootfs-entropy.ext4")
    ap.add_argument("--pause-s", type=float, default=8.0)
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="klados-spike-e-")
    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs, mem_mib=512, vcpus=1,
                     track_dirty=True, rootfs_read_only=True,
                     boot_args="console=ttyS0 reboot=k panic=1 pci=off init=/clock")
    snap = os.path.join(work, "base.snapshot")
    mem = os.path.join(work, "base.mem")

    with fc.Microvm(spec, work, name="base", console=os.path.join(work, "base.console")) as base:
        base.configure_and_start()
        time.sleep(2.0)
        base.pause()
        base.snapshot(snap, mem, diff=False)
    host_at_snapshot = time.time()

    time.sleep(args.pause_s)  # simulate a long pause (human approval, CI wait, ...)

    child_console = os.path.join(work, "child.console")
    child = fc.Microvm(spec, work, name="child", console=child_console)
    child._spawn()
    child.load(snap, mem, backend="File", resume=True)
    host_at_resume = time.time()
    time.sleep(1.5)
    child.kill()

    base_samples = parse(os.path.join(work, "base.console"))
    child_samples = parse(child_console)
    if not base_samples or not child_samples:
        raise SystemExit("no clock samples captured — check the /clock init")

    guest_wall_at_snapshot = base_samples[-1][0]
    guest_wall_after_resume = child_samples[0][0]
    real_pause = host_at_resume - host_at_snapshot
    guest_advance = guest_wall_after_resume - guest_wall_at_snapshot
    skew = host_at_resume - guest_wall_after_resume

    print(f"\n=== Spike E — clock staleness on restore ===")
    print(f"  real pause (host)            : {real_pause:.2f} s")
    print(f"  guest wall advanced over pause: {guest_advance:.2f} s")
    print(f"  guest clock skew vs host now  : {skew:.2f} s  <-- stale by ~the pause duration")
    if skew > args.pause_s * 0.5:
        print(f"\n  R8 CONFIRMED: the restored guest's clock is ~{skew:.0f}s behind true time.")
        print(f"  Pending timers/deadlines/TLS validity would be wrong. Fix: guest agent steps")
        print(f"  CLOCK_REALTIME to host true time on resume (clock_policy=step) and notifies timers.")
    else:
        print(f"\n  Guest clock tracked real time across the pause (unexpected on stock Firecracker).")


if __name__ == "__main__":
    main()
