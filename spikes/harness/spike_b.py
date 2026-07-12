#!/usr/bin/env python3
"""Spike B — CoW memory fork (tests R1). The Gate-1 fulcrum.

Snapshot a 2 GB base once, then launch N children from that one snapshot in
parallel and measure:
  1. fork wall-time  (all children resumed)                 -> target <2 s for 16
  2. memory economics via PSS                               -> Σ PSS ≪ N × base

Two mechanisms (see README §4 for why they differ):
  - file-cow : each child mmaps the same base mem file MAP_PRIVATE; kernel page
               cache shares read pages, CoW on write. Strong same-host result.
  - uffd     : a shared-base UFFD handler serves faults; savings only on untouched
               pages. This is the lazy / cross-host variant.

If neither mechanism shows sharing, R1 has failed -> Gate 1 NO-GO.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time

import fc
import metrics


def measure_base_pss(vm: fc.Microvm) -> int:
    return metrics.pss_kib(vm.pid())


def fork_file_cow(spec, work, base_snap, base_mem, n):
    """N children each load the SAME base mem file (File backend -> MAP_PRIVATE)."""
    children = []
    t0 = time.perf_counter()
    for i in range(n):
        cvm = fc.Microvm(spec, work, name=f"child_{i}")
        cvm._spawn()
        # load is synchronous per child; children still share base pages via page cache
        cvm.load(base_snap, base_mem, backend="File", resume=True)
        children.append(cvm)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return children, wall_ms


def fork_uffd(spec, work, base_snap, base_mem, n):
    """N children, each backed by a UFFD handler serving from the shared base mem file.

    Requires the Rust handler (uffd/). Each child gets its own uds; the handler
    mmaps the base once and UFFDIO_COPYs pages on fault. TODO(host): the handler
    protocol must match your Firecracker version's UFFD example — reconcile before
    trusting these numbers.
    """
    handler_bin = os.path.join(os.path.dirname(__file__), "..", "uffd", "target", "release", "klados-uffd")
    if not os.path.exists(handler_bin):
        raise SystemExit(f"uffd handler not built: {handler_bin} — run `cargo build --release --manifest-path uffd/Cargo.toml`")
    children, handlers = [], []
    t0 = time.perf_counter()
    for i in range(n):
        uds = os.path.join(work, f"uffd_{i}.sock")
        h = subprocess.Popen([handler_bin, "--socket", uds, "--mem", base_mem],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        handlers.append(h)
        # wait for handler to bind
        for _ in range(200):
            if os.path.exists(uds):
                break
            time.sleep(0.005)
        cvm = fc.Microvm(spec, work, name=f"child_{i}")
        cvm._spawn()
        cvm.load(base_snap, uds, backend="Uffd", resume=True)
        children.append(cvm)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return children, wall_ms, handlers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"))
    ap.add_argument("--rootfs", default=os.environ.get("KLADOS_ROOTFS", "/opt/klados/assets/rootfs.ext4"))
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--vcpus", type=int, default=2)
    ap.add_argument("--forks", type=int, default=16)
    ap.add_argument("--mechanism", choices=["file-cow", "uffd"], default="file-cow")
    ap.add_argument("--settle-s", type=float, default=2.0)
    ap.add_argument("--observe-s", type=float, default=1.0, help="let children run before sampling PSS")
    args = ap.parse_args()

    for p in (args.kernel, args.rootfs):
        if not os.path.exists(p):
            raise SystemExit(f"missing asset: {p} — run setup/provision.sh or pass --kernel/--rootfs")

    work = tempfile.mkdtemp(prefix="klados-spike-b-")
    # Read-only rootfs so all forks safely share one disk file (restore re-attaches the
    # original path). Memory sharing is the thesis; the disk is held constant.
    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs,
                     mem_mib=args.mem_mib, vcpus=args.vcpus, track_dirty=True,
                     rootfs_read_only=True)

    base_snap = os.path.join(work, "base.snapshot")
    base_mem = os.path.join(work, "base.mem")

    # 1) build the base snapshot
    with fc.Microvm(spec, work, name="base") as base:
        base.configure_and_start()
        time.sleep(args.settle_s)
        base_pss = measure_base_pss(base)
        base.pause()
        base.snapshot(base_snap, base_mem, diff=False)
        # base VM destroyed on context exit; children restore from the file

    # 2) fork
    handlers = []
    if args.mechanism == "file-cow":
        children, wall_ms = fork_file_cow(spec, work, base_snap, base_mem, args.forks)
    else:
        children, wall_ms, handlers = fork_uffd(spec, work, base_snap, base_mem, args.forks)

    try:
        time.sleep(args.observe_s)  # let each child touch its working set
        child_pss = [metrics.pss_kib(c.pid()) for c in children]
    finally:
        for c in children:
            c.kill()
        for h in handlers:
            if h.poll() is None:
                h.kill()

    sum_pss = sum(child_pss)
    naive = args.forks * base_pss  # what N independent VMs would cost
    savings_ratio = (sum_pss / naive) if naive else float("nan")
    verdict_time = "PASS" if wall_ms < 2000 else "FAIL"
    # "sharing observed" = children collectively cost far less than N independent VMs
    verdict_mem = "PASS" if savings_ratio < 0.5 else "FAIL"

    result = {
        "spike": "B",
        "config": vars(args),
        "mechanism": args.mechanism,
        "base_pss_kib": base_pss,
        "fork_wall_ms": round(wall_ms, 2),
        "sum_child_pss_kib": sum_pss,
        "naive_n_times_base_kib": naive,
        "savings_ratio": round(savings_ratio, 3),
        "thresholds": {"fork_wall_ms_lt": 2000, "savings_ratio_lt": 0.5},
    }

    metrics.print_table(f"Spike B — CoW fork [{args.mechanism}]", [
        ("forks", str(args.forks)),
        ("base VM PSS (MiB)", f"{base_pss/1024:.1f}"),
        ("fork wall-time (ms)", f"{result['fork_wall_ms']}   [{verdict_time}, target <2000]"),
        ("Σ child PSS (MiB)", f"{sum_pss/1024:.1f}"),
        ("naive N×base (MiB)", f"{naive/1024:.1f}"),
        ("savings ratio (ΣPSS / N×base)", f"{result['savings_ratio']}   [{verdict_mem}, want ≪ 1.0]"),
    ])
    if verdict_mem == "FAIL":
        print("\n  >> No sharing observed. If BOTH mechanisms fail this, R1 is falsified — Gate 1 is a NO-GO.")
        print("     Stop and rethink the memory model before building anything downstream.")

    metrics.save(os.path.join(os.path.dirname(__file__), "..", "results", f"spike_b_{args.mechanism}.json"), result)


if __name__ == "__main__":
    main()
