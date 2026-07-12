#!/usr/bin/env python3
"""In-guest dirty-set generator. Bake this into the rootfs and start it at boot
(e.g. a systemd unit or an init line) so Spike A's diff-snapshot numbers reflect a
working agent instead of an idle VM — this is what makes the R3 measurement honest.

It allocates a fixed buffer and continuously writes to a rolling window of pages,
so roughly --mib megabytes are dirtied per --interval seconds. Match --mib to the
--churn-mib you pass to spike_a.py.

Usage (inside the guest):  python3 churn.py --mib 512 --interval 1.0
"""
from __future__ import annotations

import argparse
import time

PAGE = 4096


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mib", type=int, default=512, help="MiB to dirty per interval")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--pool-mib", type=int, default=1024, help="size of the resident buffer")
    args = ap.parse_args()

    pool = bytearray(args.pool_mib * 1024 * 1024)
    dirty_pages = (args.mib * 1024 * 1024) // PAGE
    pool_pages = len(pool) // PAGE
    cursor = 0
    tick = 0
    while True:
        start = time.perf_counter()
        for _ in range(dirty_pages):
            pool[cursor * PAGE] = tick & 0xFF  # touch one byte per page -> whole page dirty
            cursor = (cursor + 1) % pool_pages
        tick += 1
        elapsed = time.perf_counter() - start
        if elapsed < args.interval:
            time.sleep(args.interval - elapsed)


if __name__ == "__main__":
    main()
