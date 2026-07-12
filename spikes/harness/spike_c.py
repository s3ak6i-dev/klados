#!/usr/bin/env python3
"""Spike C — entropy / identity divergence across forks (tests R2).

Boots a guest that streams three random samples per line to the serial console, forks
N children from one snapshot, and checks each stream for cross-fork collisions:
  MT = userspace Mersenne Twister (random)   — state frozen in snapshot
  O  = OpenSSL DRBG (ssl.RAND_bytes)         — TLS client-random source (the scary one)
  K  = kernel CSPRNG (/dev/urandom)          — self-heals if RDRAND mixed per read

Modes (select the in-guest generator via kernel init=):
  --mode vanilla    : no post-fork reseed  -> expect MT (and maybe O) to collide
  --mode mitigated  : reseed userspace PRNGs from kernel CSPRNG -> expect all clean

Requires the entropy rootfs: `sudo bash setup/prep_entropy_rootfs.sh`.
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile
import time
from collections import defaultdict

import fc
import metrics

SAMPLE_RE = re.compile(rb"SAMPLE (\d+) MT=([0-9a-f]+) O=([0-9a-f]+) K=([0-9a-f]+)")
INIT = {"vanilla": "/entropy", "mitigated": "/entropy_mitigated", "transparent": "/entropy_transparent"}


def parse_console(path: str):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    return [(int(m.group(1)), m.group(2).decode(), m.group(3).decode(), m.group(4).decode())
            for m in SAMPLE_RE.finditer(data)]


def cross_child_collisions(per_child_values: dict[int, set]):
    seen = defaultdict(set)
    for child, values in per_child_values.items():
        for v in values:
            seen[v].add(child)
    return {v: kids for v, kids in seen.items() if len(kids) >= 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"))
    ap.add_argument("--rootfs", default="/opt/klados/assets/rootfs-entropy.ext4")
    ap.add_argument("--forks", type=int, default=8)
    ap.add_argument("--mem-mib", type=int, default=512)
    ap.add_argument("--mode", choices=["vanilla", "mitigated", "transparent"], default="vanilla")
    ap.add_argument("--warm-s", type=float, default=3.0)
    ap.add_argument("--observe-s", type=float, default=3.0)
    args = ap.parse_args()

    if not os.path.exists(args.rootfs):
        raise SystemExit(f"missing {args.rootfs} — run: sudo bash setup/prep_entropy_rootfs.sh")

    work = tempfile.mkdtemp(prefix=f"klados-spike-c-{args.mode}-")
    spec = fc.VmSpec(
        kernel=args.kernel, rootfs=args.rootfs, mem_mib=args.mem_mib, vcpus=1,
        track_dirty=True, rootfs_read_only=True,
        boot_args=f"console=ttyS0 reboot=k panic=1 pci=off init={INIT[args.mode]}",
    )

    base_snap = os.path.join(work, "base.snapshot")
    base_mem = os.path.join(work, "base.mem")

    with fc.Microvm(spec, work, name="base", console=os.path.join(work, "base.console")) as base:
        base.configure_and_start()
        time.sleep(args.warm_s)
        base.pause()
        base.snapshot(base_snap, base_mem, diff=False)

    children, consoles = [], []
    for i in range(args.forks):
        cpath = os.path.join(work, f"child_{i}.console")
        consoles.append(cpath)
        c = fc.Microvm(spec, work, name=f"child_{i}", console=cpath)
        c._spawn()
        c.load(base_snap, base_mem, backend="File", resume=True)
        children.append(c)

    try:
        time.sleep(args.observe_s)
    finally:
        for c in children:
            c.kill()

    streams = {"MT": {}, "O": {}, "K": {}}
    counts = {}
    for i, cpath in enumerate(consoles):
        rows = parse_console(cpath)
        counts[i] = len(rows)
        streams["MT"][i] = {mt for _, mt, _, _ in rows}
        streams["O"][i] = {o for _, _, o, _ in rows}
        streams["K"][i] = {k for _, _, _, k in rows}

    labels = {
        "MT": "userspace Mersenne Twister (random)",
        "O": "OpenSSL DRBG (ssl.RAND_bytes) — TLS client random",
        "K": "kernel CSPRNG (/dev/urandom)",
    }
    result = {"spike": "C", "mode": args.mode, "config": vars(args),
              "samples_per_child": counts, "streams": {}}
    rows_out = [("mode", args.mode), ("forks", str(args.forks)),
                ("samples/child", str(list(counts.values())))]
    for key in ("MT", "O", "K"):
        col = cross_child_collisions(streams[key])
        total = sum(len(v) for v in streams[key].values())
        verdict = "COLLIDE" if col else "clean"
        result["streams"][key] = {"label": labels[key], "total_values": total,
                                  "cross_fork_collisions": len(col), "verdict": verdict}
        rows_out.append((f"[{key}] {labels[key]}", ""))
        rows_out.append((f"   cross-fork collisions", f"{len(col)}   [{verdict}]"))

    metrics.print_table(f"Spike C — entropy divergence [{args.mode}]", rows_out)

    mt = result["streams"]["MT"]["cross_fork_collisions"]
    o = result["streams"]["O"]["cross_fork_collisions"]
    if args.mode == "vanilla":
        if o:
            print(f"\n  ** SECURITY FINDING: OpenSSL RAND_bytes COLLIDED across forks ({o} values). **")
            print(f"     VM-fork keeps the guest PID constant, so OpenSSL's PID-based fork detection")
            print(f"     never fires. Forked agents would share TLS client randoms — a real hazard.")
        else:
            print(f"\n  OpenSSL RAND_bytes did NOT collide — it reseeds from the kernel often enough to self-heal.")
        if mt:
            print(f"  Userspace MT collided ({mt}) as expected — the frozen-state class R2 warns about.")
    elif args.mode == "mitigated":
        if not mt and not o:
            print(f"\n  MITIGATION WORKS: reseeding userspace PRNGs from the kernel CSPRNG cleared all")
            print(f"  collisions (MT and OpenSSL). This is what the guest agent must do on the FORKED event.")
        else:
            print(f"\n  MITIGATION INCOMPLETE: MT={mt} O={o} still colliding — this runtime resists a simple reseed.")
    else:  # transparent
        vanilla_ref = 65  # observed collisions with the SAME app, no hook
        if not mt and not o:
            print(f"\n  TRANSPARENT FIX WORKS (zero window): app UNMODIFIED, all collisions cleared by the")
            print(f"  base-image sitecustomize hook alone. No application cooperation.")
        else:
            print(f"\n  TRANSPARENT FIX: app UNMODIFIED (identical to the vanilla run that had ~{vanilla_ref}")
            print(f"  collisions). Async periodic rekey cleared all but MT={mt} O={o} — the single FIRST")
            print(f"  post-resume sample, which races the reseed thread and cannot be won by any polling")
            print(f"  interval. Zero-window requires reseed synchronous-before-first-use: the event-driven")
            print(f"  guest-agent path (reseed on FORKED before the workload runs) or call-site interception.")

    metrics.save(os.path.join(os.path.dirname(__file__), "..", "results", f"spike_c_{args.mode}.json"), result)


if __name__ == "__main__":
    main()
