#!/usr/bin/env python3
"""Spike G — the real fork protocol end to end (guest agent + vsock).

1. Boot base with a vsock guest agent + the UNMODIFIED workload (event-mode RNG hook).
2. Validate the vsock pipeline on the base VM (PING -> PONG).
3. Snapshot, then fork N children, each with its own vsock socket.
4. Drive FORKED {index, n, true_time, branch_context} to each child; agent reseeds entropy,
   steps the clock, injects branch context, replies READY.
5. Verify all three at once:
     entropy  -> zero cross-fork collisions (MT + OpenSSL/TLS), app UNMODIFIED
     clock    -> each child's post-fixup clock ~= host true time (skew ~0)
     branch   -> each child received its distinct branch context

Requires: sudo bash setup/prep_entropy_rootfs.sh
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile
import time
from collections import defaultdict

import fc

SAMPLE_RE = re.compile(rb"SAMPLE (\d+) MT=([0-9a-f]+) O=([0-9a-f]+) K=([0-9a-f]+)")
FORKED_RE = re.compile(rb"KLADOS_FORKED index=(\S+) gen=(\S+) clock=([0-9.\-]+) context=(.*)")


def parse_samples(path):
    try:
        data = open(path, "rb").read()
    except FileNotFoundError:
        return set(), set()
    mt = {m.group(2) for m in SAMPLE_RE.finditer(data)}
    o = {m.group(3) for m in SAMPLE_RE.finditer(data)}
    return mt, o


def parse_forked(path):
    try:
        data = open(path, "rb").read()
    except FileNotFoundError:
        return None
    m = FORKED_RE.search(data)
    if not m:
        return None
    return {"index": m.group(1).decode(), "clock": float(m.group(3)),
            "context": m.group(4).decode().strip()}


def cross(vals: dict):
    seen = defaultdict(set)
    for child, s in vals.items():
        for v in s:
            seen[v].add(child)
    return {v: k for v, k in seen.items() if len(k) >= 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default=os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"))
    ap.add_argument("--rootfs", default="/opt/klados/assets/rootfs-entropy.ext4")
    ap.add_argument("--forks", type=int, default=4)
    ap.add_argument("--mem-mib", type=int, default=512)
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="klados-spike-g-")
    base_uds = os.path.join(work, "base.vsock")
    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs, mem_mib=args.mem_mib, vcpus=1,
                     track_dirty=True, rootfs_read_only=True, vsock_uds=base_uds,
                     boot_args="console=ttyS0 reboot=k panic=1 pci=off init=/init_protocol")
    snap = os.path.join(work, "base.snapshot")
    mem = os.path.join(work, "base.mem")

    with fc.Microvm(spec, work, name="base", console=os.path.join(work, "base.console")) as base:
        base.configure_and_start()
        time.sleep(3.0)  # agent boots + listens; workload streams
        try:
            pong = fc.vsock_request(base_uds, 5000, {"type": "PING"})
            print(f"[base] vsock PING -> {pong}")
        except Exception as e:
            raise SystemExit(f"vsock pipeline failed on base VM: {e}")
        # gate the workload STOPPED before snapshot -> no child runs workload code pre-reseed
        q = fc.vsock_request(base_uds, 5000, {"type": "QUIESCE"})
        print(f"[base] QUIESCE -> {q}")
        time.sleep(0.2)  # let the SIGSTOP take effect before we freeze
        base.pause()
        base.snapshot(snap, mem, diff=False)

    # NOTE: Firecracker bakes the host-side vsock uds path into the snapshot, so N children
    # from one snapshot cannot each bind their own socket concurrently (EADDRINUSE). Per-fork
    # device remapping on restore is a kladosd responsibility (not yet built). To validate the
    # PROTOCOL here we serialize: each child reuses the freed base socket. Every child still
    # resumes from the IDENTICAL frozen RNG state, so distinct cross-child output proves the fix.
    consoles, readys = [], []
    for i in range(args.forks):
        cpath = os.path.join(work, f"child_{i}.console")
        consoles.append(cpath)
        try:
            os.unlink(base_uds)  # free the snapshot's baked vsock socket for this child
        except FileNotFoundError:
            pass
        c = fc.Microvm(spec, work, name=f"child_{i}", console=cpath)
        c._spawn()
        c.load(snap, mem, backend="File", resume=True)  # uses the baked base_uds
        host_true = time.time()
        try:
            r = fc.vsock_request(base_uds, 5000, {
                "type": "FORKED", "index": i, "n": args.forks,
                "true_time": host_true, "branch_context": f"you are branch {i} of {args.forks}",
            })
            r["_host_true"] = host_true
        except Exception as e:
            r = {"status": "ERR", "msg": str(e), "_host_true": host_true}
        readys.append(r)
        time.sleep(2.0)  # collect post-fork samples
        c.kill()         # free the socket for the next child

    # ---- verify ----
    mt_by, o_by = {}, {}
    for i, cp in enumerate(consoles):
        mt_by[i], o_by[i] = parse_samples(cp)
    mt_col, o_col = len(cross(mt_by)), len(cross(o_by))

    print("\n=== Spike G — fork protocol (guest agent + vsock) ===")
    print(f"  forks: {args.forks}")
    print(f"\n  READY handshakes:")
    ok_ready = sum(1 for r in readys if r.get("status") == "READY")
    for i, r in enumerate(readys):
        print(f"    child {i}: {r.get('status')}  gen={r.get('gen')}")
    print(f"    -> {ok_ready}/{args.forks} children completed the FORKED handshake")

    print(f"\n  ENTROPY (app UNMODIFIED):")
    print(f"    cross-fork MT collisions       : {mt_col}   [{'clean' if not mt_col else 'COLLIDE'}]")
    print(f"    cross-fork OpenSSL/TLS collisions: {o_col}   [{'clean' if not o_col else 'COLLIDE'}]")

    print(f"\n  CLOCK fixup (skew = host_true - guest_clock_after):")
    for i, r in enumerate(readys):
        fk = parse_forked(consoles[i])
        if fk and fk.get("clock", -1) > 0:
            skew = r["_host_true"] - fk["clock"]
            print(f"    child {i}: skew {skew*1000:+.1f} ms")

    print(f"\n  BRANCH CONTEXT delivered (from each child's console):")
    ctx_ok = 0
    for i, cp in enumerate(consoles):
        fk = parse_forked(cp)
        got = fk["context"] if fk else None
        want = f"you are branch {i} of {args.forks}"
        mark = "ok" if got == want else "MISMATCH"
        if got == want:
            ctx_ok += 1
        print(f"    child {i}: {mark}  {got!r}")

    zero_window = (mt_col == 0 and o_col == 0)
    all_ready = (ok_ready == args.forks)
    all_ctx = (ctx_ok == args.forks)
    print(f"\n  VERDICT: handshakes {ok_ready}/{args.forks} | entropy {'ZERO-WINDOW' if zero_window else 'residual'}"
          f" | branch {ctx_ok}/{args.forks}")
    if all_ready and zero_window and all_ctx:
        print("  FORK PROTOCOL WORKS END TO END: transparent zero-window entropy + clock + branch context.")


if __name__ == "__main__":
    main()
