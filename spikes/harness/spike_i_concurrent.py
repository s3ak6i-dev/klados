#!/usr/bin/env python3
"""Spike I — CONCURRENT N-way fork with per-fork device remapping (M0 blocker).

Firecracker bakes the host-side vsock socket path into the snapshot, so N children restoring
from one snapshot collide on it. This runs each fork's Firecracker in its own mount namespace
where the baked dir is bind-mounted to a per-fork directory — so every child's baked vsock path
resolves to its own socket, and all N can run AT THE SAME TIME.

Proves: the full fork protocol (zero-window entropy + clock + branch context) works with all
children alive concurrently, not serialized.

Run as ROOT (mount namespaces): wsl -d Ubuntu -u root -- python3 spike_i_concurrent.py
Requires: sudo bash setup/prep_entropy_rootfs.sh
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict

import fc

SAMPLE_RE = re.compile(rb"SAMPLE (\d+) MT=([0-9a-f]+) O=([0-9a-f]+) K=([0-9a-f]+)")
FORKED_RE = re.compile(rb"KLADOS_FORKED index=(\S+) gen=(\S+) clock=([0-9.\-]+) context=(.*)")
BAKED = "/klados-vsock"


def parse_samples(path):
    try:
        data = open(path, "rb").read()
    except FileNotFoundError:
        return set(), set()
    return ({m.group(2) for m in SAMPLE_RE.finditer(data)},
            {m.group(3) for m in SAMPLE_RE.finditer(data)})


def parse_forked(path):
    try:
        m = FORKED_RE.search(open(path, "rb").read())
    except FileNotFoundError:
        return None
    return {"clock": float(m.group(3)), "context": m.group(4).decode().strip()} if m else None


def cross(vals):
    seen = defaultdict(set)
    for c, s in vals.items():
        for v in s:
            seen[v].add(c)
    return {v: k for v, k in seen.items() if len(k) >= 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default="/opt/klados/assets/vmlinux")
    ap.add_argument("--rootfs", default="/opt/klados/assets/rootfs-entropy.ext4")
    ap.add_argument("--forks", type=int, default=6)
    ap.add_argument("--mem-mib", type=int, default=512)
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="klados-spike-i-")
    os.makedirs(BAKED, exist_ok=True)
    base_uds = os.path.join(BAKED, "vm.vsock")
    try:
        os.unlink(base_uds)
    except FileNotFoundError:
        pass

    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs, mem_mib=args.mem_mib, vcpus=1,
                     track_dirty=True, rootfs_read_only=True, vsock_uds=base_uds,
                     boot_args="console=ttyS0 reboot=k panic=1 pci=off init=/init_protocol")
    snap = os.path.join(work, "base.snapshot")
    mem = os.path.join(work, "base.mem")

    # base: warm, quiesce (stop workload for zero-window), snapshot
    with fc.Microvm(spec, work, name="base", console=os.path.join(work, "base.console")) as base:
        base.configure_and_start()
        time.sleep(3.0)
        print(f"[base] PING -> {fc.vsock_request(base_uds, 5000, {'type': 'PING'})}")
        fc.vsock_request(base_uds, 5000, {"type": "QUIESCE"})
        time.sleep(0.2)
        base.pause()
        base.snapshot(snap, mem, diff=False)

    # fork ALL children concurrently, each in its own mount namespace (per-fork vsock)
    children, uds_paths, consoles = [], [], []
    t0 = time.perf_counter()
    for i in range(args.forks):
        perfork = os.path.join(work, f"fork_{i}")
        cpath = os.path.join(work, f"child_{i}.console")
        consoles.append(cpath)
        uds_paths.append(os.path.join(perfork, "vm.vsock"))
        c = fc.Microvm(spec, work, name=f"child_{i}", console=cpath,
                       vsock_remap=(BAKED, perfork))
        c._spawn()
        c.load(snap, mem, backend="File", resume=True)
        children.append(c)
    spawn_ms = (time.perf_counter() - t0) * 1000

    # all N are now alive AT THE SAME TIME; drive the protocol on each
    alive_concurrently = sum(1 for c in children if c.proc.poll() is None)
    readys = []
    for i, c in enumerate(children):
        host_true = time.time()
        try:
            r = fc.vsock_request(uds_paths[i], 5000, {
                "type": "FORKED", "index": i, "n": args.forks,
                "true_time": host_true, "branch_context": f"you are branch {i} of {args.forks}"})
            r["_host_true"] = host_true
        except Exception as e:
            r = {"status": "ERR", "msg": str(e), "_host_true": host_true}
        readys.append(r)

    time.sleep(3.0)
    still_alive = sum(1 for c in children if c.proc.poll() is None)
    for c in children:
        c.kill()
    # (parse the consoles BEFORE cleaning up the temp dir they live in)

    # verify
    mt_by, o_by = {}, {}
    for i, cp in enumerate(consoles):
        mt_by[i], o_by[i] = parse_samples(cp)
    mt_col, o_col = len(cross(mt_by)), len(cross(o_by))
    ok_ready = sum(1 for r in readys if r.get("status") == "READY")
    ctx_ok = sum(1 for i, cp in enumerate(consoles)
                 if (parse_forked(cp) or {}).get("context") == f"you are branch {i} of {args.forks}")

    print(f"\n=== Spike I — concurrent {args.forks}-way fork (per-fork vsock remap) ===")
    print(f"  children alive concurrently (post-spawn) : {alive_concurrently}/{args.forks}")
    print(f"  children still alive after protocol       : {still_alive}/{args.forks}")
    print(f"  concurrent spawn+load wall-time           : {spawn_ms:.0f} ms (nested virt — not real perf)")
    print(f"  FORKED handshakes                         : {ok_ready}/{args.forks}")
    print(f"  cross-fork entropy collisions (MT / TLS)  : {mt_col} / {o_col}")
    print(f"  branch context delivered                  : {ctx_ok}/{args.forks}")
    skews = []
    for i, r in enumerate(readys):
        fk = parse_forked(consoles[i]) if i < len(consoles) else None
        if fk and fk.get("clock", -1) > 0:
            skews.append(abs(r["_host_true"] - fk["clock"]) * 1000)
    if skews:
        print(f"  clock skew (max)                          : {max(skews):.1f} ms")

    concurrent = (alive_concurrently == args.forks and still_alive == args.forks)
    if concurrent and ok_ready == args.forks and mt_col == 0 and o_col == 0 and ctx_ok == args.forks:
        print(f"\n  CONCURRENT FORK WORKS: {args.forks} children ran simultaneously, each with its own")
        print(f"  vsock, full protocol (zero-window entropy + clock + branch) on all. M0 blocker cleared.")
    else:
        print(f"\n  Incomplete — see counts above.")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
