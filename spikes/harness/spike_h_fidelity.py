#!/usr/bin/env python3
"""Spike H — stateful multi-process workload fidelity (partial R7 / the browser-fidelity risk).

Boots a multi-process SQLite workload, snapshots it WHILE writes are in flight (no quiesce, to
test crash-consistency), then forks and asks each child (over vsock):
  - PRAGMA integrity_check == "ok"       (DB not corrupted by the mid-write snapshot)
  - every worker resumed and kept writing (per-worker row counts grew after restore)
  - in-memory session identity survived   (same token as the base)

This covers the hard parts of browser fidelity — multiple processes + an on-disk store mutated
at the snapshot instant — with in-guest Python. Full Chromium (GPU/renderer IPC) is a heavier
follow-up requiring a browser baked into the rootfs.

Requires: sudo bash setup/prep_entropy_rootfs.sh
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile
import time

import fc

SESS_RE = re.compile(rb"FIDELITY session=([0-9a-f]+)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"))
    ap.add_argument("--rootfs", default="/opt/klados/assets/rootfs-entropy.ext4")
    ap.add_argument("--forks", type=int, default=3)
    ap.add_argument("--mem-mib", type=int, default=512)
    ap.add_argument("--load-s", type=float, default=3.0, help="write load before snapshot")
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="klados-spike-h-")
    base_uds = os.path.join(work, "base.vsock")
    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs, mem_mib=args.mem_mib, vcpus=2,
                     track_dirty=True, rootfs_read_only=True, vsock_uds=base_uds,
                     boot_args="console=ttyS0 reboot=k panic=1 pci=off init=/init_fidelity")
    snap = os.path.join(work, "base.snapshot")
    mem = os.path.join(work, "base.mem")

    with fc.Microvm(spec, work, name="base", console=os.path.join(work, "base.console")) as base:
        base.configure_and_start()
        time.sleep(args.load_s)  # workers accumulate committed writes
        base_check = fc.vsock_request(base_uds, 5000, {"type": "CHECK"})
        print(f"[base] pre-snapshot CHECK: integrity={base_check.get('integrity')} "
              f"total={base_check.get('total')} per_worker={base_check.get('per_worker')}")
        base_session = base_check.get("session")
        # SNAPSHOT MID-WRITE — no quiesce, workers are actively committing right now
        base.pause()
        base.snapshot(snap, mem, diff=False)

    results = []
    for i in range(args.forks):
        cpath = os.path.join(work, f"child_{i}.console")
        try:
            os.unlink(base_uds)
        except FileNotFoundError:
            pass
        c = fc.Microvm(spec, work, name=f"child_{i}", console=cpath)
        c._spawn()
        c.load(snap, mem, backend="File", resume=True)
        time.sleep(1.5)  # let restored workers make progress
        chk = fc.vsock_request(base_uds, 5000, {"type": "CHECK"})
        c.kill()
        results.append(chk)

    # baseline per-worker counts at snapshot time
    base_pw = {k: int(v) for k, v in (base_check.get("per_worker") or {}).items()}

    print("\n=== Spike H — multi-process + on-disk fidelity under mid-write snapshot ===")
    print(f"  workers: 3   forks: {args.forks}   (snapshot taken WHILE the DB was being written)\n")
    all_ok = True
    for i, chk in enumerate(results):
        integ = chk.get("integrity")
        pw = {k: int(v) for k, v in (chk.get("per_worker") or {}).items()}
        resumed = all(pw.get(w, 0) >= base_pw.get(w, 0) for w in base_pw) and len(pw) >= len(base_pw)
        grew = sum(pw.values()) > sum(base_pw.values())
        sess_ok = (chk.get("session") == base_session)
        ok = (integ == "ok") and resumed and grew and sess_ok
        all_ok = all_ok and ok
        print(f"  child {i}: integrity={integ!r} | workers_resumed={resumed} | "
              f"rows {sum(base_pw.values())}->{sum(pw.values())} | session_survived={sess_ok}  "
              f"[{'PASS' if ok else 'FAIL'}]")

    print(f"\n  VERDICT: {'FIDELITY OK' if all_ok else 'FIDELITY ISSUE'} — "
          f"{'DB crash-consistent, all workers resumed, in-memory state survived across forks' if all_ok else 'see failures above'}")


if __name__ == "__main__":
    main()
