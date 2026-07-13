#!/usr/bin/env python3
"""Spike L — on-disk snapshot storage dedup (M2). Validates the PRD's >=8x target.

We proved LIVE memory sharing (PSS) in spike_b. This proves STORED-snapshot dedup: build a fork
tree, then run every snapshot's mem + disk artifacts through the content-addressed store and
measure logical bytes vs. stored (deduped + compressed) bytes.

Run as ROOT (boots VMs). Requires kladosd running.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sdk"))
sys.path.insert(0, HERE)
from klados import Sandbox
from cas import CAS

SNAPDIR = "/var/lib/klados/snapshots"
FORKS = int(sys.argv[1]) if len(sys.argv) > 1 else 4


def main():
    print("building a fork tree (boots VMs, snapshots each)…")
    sb = Sandbox.create()
    base = sb.snapshot(label="task loaded")
    kids = base.fork(FORKS)
    snap_ids = [base.id]
    for i, c in enumerate(kids):
        snap_ids.append(c.snapshot(label=f"branch {i}").id)
    print(f"  {len(snap_ids)} snapshots created")

    cas = CAS(block=4096)
    artifacts = 0
    for sid in snap_ids:
        for name in ("mem", "scratch.ext4"):
            p = os.path.join(SNAPDIR, sid, name)
            if os.path.exists(p):
                cas.put_file(p)
                artifacts += 1

    st = cas.stats()
    gb = lambda b: b / 1e9
    print("\n=== Spike L — snapshot storage dedup (content-addressed, 4 KiB blocks) ===")
    print(f"  snapshots               : {len(snap_ids)}  ({artifacts} artifacts)")
    print(f"  logical bytes           : {gb(st['logical_bytes']):.2f} GB")
    print(f"  chunks total / unique   : {st['chunks_total']:,} / {st['chunks_unique']:,}  "
          f"({st['unique_fraction']*100:.1f}% unique)")
    print(f"  stored (dedup + zlib)   : {gb(st['stored_bytes']):.3f} GB")
    print(f"  DEDUP RATIO             : {st['dedup_ratio']}x   [PRD target >= 8x]")
    verdict = "MEETS" if st["dedup_ratio"] >= 8 else "below"
    print(f"\n  {verdict} the >=8x target: {len(snap_ids)} full snapshots stored in "
          f"{gb(st['stored_bytes'])*1000:.0f} MB instead of {gb(st['logical_bytes']):.1f} GB.")

    for c in kids:
        try:
            c.destroy()
        except Exception:
            pass
    sb.destroy()


if __name__ == "__main__":
    main()
