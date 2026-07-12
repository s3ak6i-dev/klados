#!/usr/bin/env python3
"""Spike A — Firecracker baseline latency (tests R3).

Measures the two numbers Gate-A hinges on:
  - snapshot PAUSE WINDOW (pause -> create -> resume), full and diff, under churn
  - WARM RESTORE (load a fresh VM from the snapshot until it's resumed)

Pass: diff-snapshot pause p50 < 250 ms; warm restore p50 < 500 ms.

The diff number is only meaningful under a realistic dirty set — pass --churn-mib
to size it. Idle-VM snapshots look great and prove nothing (see R3 in the
pressure-test doc). Realistic churn requires the churn generator baked into the
rootfs; see workload/churn.py. Absent that, this measures snapshot mechanics on
whatever the guest happens to be doing and flags the caveat in the output.
"""
from __future__ import annotations

import argparse
import os
import tempfile
import time

import fc
import metrics


def env_default(name, fallback):
    return os.environ.get(name, fallback)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=env_default("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"))
    ap.add_argument("--rootfs", default=env_default("KLADOS_ROOTFS", "/opt/klados/assets/rootfs.ext4"))
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--vcpus", type=int, default=2)
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--churn-mib", type=int, default=0,
                    help="informational: expected dirty set per iter (bake churn into rootfs to realize it)")
    ap.add_argument("--settle-s", type=float, default=2.0, help="boot settle time before measuring")
    args = ap.parse_args()

    for p in (args.kernel, args.rootfs):
        if not os.path.exists(p):
            raise SystemExit(f"missing asset: {p} — run setup/provision.sh or pass --kernel/--rootfs")

    work = tempfile.mkdtemp(prefix="klados-spike-a-")
    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs,
                     mem_mib=args.mem_mib, vcpus=args.vcpus, track_dirty=True)

    pause_full, pause_diff, restore_ms = [], [], []

    with fc.Microvm(spec, work, name="base") as vm:
        vm.configure_and_start()
        time.sleep(args.settle_s)  # let boot/init reach steady state

        # First snapshot must be Full (it establishes the dirty-tracking baseline).
        for i in range(args.iterations):
            diff = i > 0  # iter 0 = Full, rest = Diff
            snap = os.path.join(work, f"snap_{i}.snapshot")
            mem = os.path.join(work, f"snap_{i}.mem")

            t0 = time.perf_counter()
            vm.pause()
            vm.snapshot(snap, mem, diff=diff)
            vm.resume()
            pause_ms = (time.perf_counter() - t0) * 1000.0
            (pause_diff if diff else pause_full).append(pause_ms)

            # Warm restore: chunks are fresh in host page cache -> best case.
            snap0 = os.path.join(work, "snap_0.snapshot")
            mem0 = os.path.join(work, "snap_0.mem")
            with fc.Microvm(spec, work, name=f"restore_{i}") as rvm:
                t1 = time.perf_counter()
                rvm.load(snap0, mem0, backend="File", resume=True)
                restore_ms.append((time.perf_counter() - t1) * 1000.0)

            # keep only the base full snapshot around; drop per-iter diffs to save disk
            if diff:
                for f in (snap, mem):
                    try: os.unlink(f)
                    except FileNotFoundError: pass

    result = {
        "spike": "A",
        "config": vars(args),
        "pause_full_ms": metrics.summary(pause_full),
        "pause_diff_ms": metrics.summary(pause_diff),
        "warm_restore_ms": metrics.summary(restore_ms),
        "thresholds": {"pause_diff_p50_lt": 250, "warm_restore_p50_lt": 500},
    }

    d, r = result["pause_diff_ms"], result["warm_restore_ms"]
    verdict_pause = "PASS" if (d["p50"] or 1e9) < 250 else "FAIL"
    verdict_rest = "PASS" if (r["p50"] or 1e9) < 500 else "FAIL"
    metrics.print_table("Spike A — baseline latency", [
        ("full snapshot pause p50 (ms)", str(result["pause_full_ms"]["p50"])),
        ("diff snapshot pause p50 (ms)", f'{d["p50"]}   [{verdict_pause}, target <250]'),
        ("diff snapshot pause p99 (ms)", str(d["p99"])),
        ("warm restore p50 (ms)", f'{r["p50"]}   [{verdict_rest}, target <500]'),
        ("warm restore p99 (ms)", str(r["p99"])),
    ])
    if args.churn_mib == 0:
        print("\n  CAVEAT: --churn-mib 0 — diff numbers reflect an ~idle guest and overstate the pass.")
        print("  Bake workload/churn.py into the rootfs and set --churn-mib to get the number R3 depends on.")

    metrics.save(os.path.join(os.path.dirname(__file__), "..", "results", "spike_a.json"), result)


if __name__ == "__main__":
    main()
