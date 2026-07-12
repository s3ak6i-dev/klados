#!/usr/bin/env python3
"""Spike F — validate the incremental-snapshot ALGORITHM (part of R3), hardware-independent.

Absolute snapshot/restore latency is corrupted by nested virt and needs bare metal. But the
CLAIM behind the <250 ms pause target — "diff snapshots write only dirty pages" (FR1.3) — is
about BYTES WRITTEN, which is hardware-independent. This measures the physical bytes of a full
snapshot vs a diff snapshot taken after brief idle. If diff << full, the incremental algorithm
works and the pause-time target is a hardware question, not an algorithm question.

Wall-times are printed too but LABELLED unreliable (nested virt).
"""
from __future__ import annotations

import os
import tempfile
import time

import fc


def phys_bytes(path):
    st = os.stat(path)
    return st.st_blocks * 512  # actually-allocated blocks (sparse-aware), not logical size


def main():
    kernel = os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux")
    rootfs = os.environ.get("KLADOS_ROOTFS", "/opt/klados/assets/rootfs.ext4")
    work = tempfile.mkdtemp(prefix="klados-spike-f-")
    spec = fc.VmSpec(kernel=kernel, rootfs=rootfs, mem_mib=512, vcpus=1, track_dirty=True,
                     rootfs_read_only=True)  # memory-only spike; share the base RO

    fmem = os.path.join(work, "full.mem")
    dmem = os.path.join(work, "diff.mem")

    with fc.Microvm(spec, work, name="base") as vm:
        vm.configure_and_start()
        time.sleep(2.0)

        t0 = time.perf_counter()
        vm.pause(); vm.snapshot(os.path.join(work, "full.snap"), fmem, diff=False); vm.resume()
        full_pause_ms = (time.perf_counter() - t0) * 1000

        time.sleep(1.0)  # brief idle -> only a few pages dirtied

        t1 = time.perf_counter()
        vm.pause(); vm.snapshot(os.path.join(work, "diff.snap"), dmem, diff=True); vm.resume()
        diff_pause_ms = (time.perf_counter() - t1) * 1000

    full_p, diff_p = phys_bytes(fmem), phys_bytes(dmem)
    logical = os.path.getsize(fmem)
    ratio = diff_p / full_p if full_p else float("nan")

    print("\n=== Spike F — incremental snapshot algorithm ===")
    print(f"  logical mem image size     : {logical/1e6:.1f} MB (both files, sparse)")
    print(f"  FULL snapshot bytes written : {full_p/1e6:.1f} MB (physical/allocated)")
    print(f"  DIFF snapshot bytes written : {diff_p/1e6:.2f} MB (physical/allocated)")
    print(f"  diff/full byte ratio        : {ratio:.4f}   [want << 1.0]")
    print(f"\n  wall-times (NESTED VIRT — not real perf, do not compare to thresholds):")
    print(f"    full pause ~{full_pause_ms:.0f} ms | diff pause ~{diff_pause_ms:.0f} ms")
    if ratio < 0.2:
        print(f"\n  ALGORITHM VALIDATED: diff writes {ratio*100:.1f}% of full's bytes. The <250ms pause")
        print(f"  target is now a hardware question (write {diff_p/1e6:.1f}MB fast), not an algorithm one.")
    else:
        print(f"\n  Diff not much smaller than full — investigate dirty-page tracking (track_dirty_pages).")


if __name__ == "__main__":
    main()
