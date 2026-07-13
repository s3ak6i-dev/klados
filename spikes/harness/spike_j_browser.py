#!/usr/bin/env python3
"""Spike J — full browser fidelity: does a LIVE headless-Chrome session survive snapshot + fork?

Boots a VM running headless Chrome with a session token set in the JS heap + localStorage, then
snapshots and concurrently forks it. For each fork it asks (over vsock) for the live browser state:
  - token == base token           -> the JS heap / renderer survived the fork (not just disk)
  - heartbeat still advancing      -> Chrome is alive and responsive AFTER the fork
  - localStorage matches           -> browser storage intact

This is the D2 differentiator ("browser state captured for free"), the piece Spike H's Python+DB
workload could not cover (GPU/renderer processes, CDP, JS heap).

Run as ROOT (mount-namespace fork). Requires: sudo bash setup/build_browser_rootfs.sh
"""
from __future__ import annotations

import argparse
import os
import re
import time

import fc

BAKED = "/klados-vsock"


def bstate(uds):
    try:
        r = fc.vsock_request(uds, 5000, {"type": "BROWSER_CHECK"}, timeout=15)
    except Exception as e:
        return None, {"error": str(e)}
    st = r.get("state") or ""
    m = re.search(r"token=(\S+) beat=(\S+) ls=(\S+)", st)
    parsed = {"token": m.group(1), "beat": m.group(2), "ls": m.group(3)} if m else {"raw": st}
    return r.get("token"), parsed


def wait_agent(uds, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fc.vsock_request(uds, 5000, {"type": "PING"}, timeout=5).get("status") == "PONG":
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def wait_browser(uds, timeout=150):
    """Chrome is slow to boot in a microVM — wait until it has published a token."""
    end = time.time() + timeout
    while time.time() < end:
        tok, st = bstate(uds)
        if tok:
            return tok
        print(f"    …chrome not ready: {st}")
        time.sleep(4.0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel", default="/opt/klados/assets/vmlinux")
    ap.add_argument("--rootfs", default="/opt/klados/assets/rootfs-browser.ext4")
    ap.add_argument("--forks", type=int, default=3)
    ap.add_argument("--mem-mib", type=int, default=2048)
    args = ap.parse_args()

    import tempfile
    scratch = "/var/lib/klados/scratch"  # disk-backed (NOT the small tmpfs /tmp) — mem files are big
    os.makedirs(scratch, exist_ok=True)
    work = tempfile.mkdtemp(prefix="klados-spike-j-", dir=scratch)
    os.makedirs(BAKED, exist_ok=True)
    base_dir = os.path.join(work, "base")
    base_uds = os.path.join(base_dir, "vm.vsock")
    spec = fc.VmSpec(kernel=args.kernel, rootfs=args.rootfs, mem_mib=args.mem_mib, vcpus=2,
                     track_dirty=True, rootfs_read_only=True, vsock_uds=BAKED + "/vm.vsock",
                     boot_args="console=ttyS0 reboot=k panic=1 pci=off init=/init_browser")
    snap = os.path.join(work, "base.snapshot")
    mem = os.path.join(work, "base.mem")

    base = fc.Microvm(spec, work, name="base", console=os.path.join(work, "base.console"),
                      vsock_remap=(BAKED, base_dir))
    base._spawn()
    base.configure_and_start()
    print("[base] waiting for guest agent…")
    if not wait_agent(base_uds):
        raise SystemExit("agent never came up — check base console")
    print("[base] waiting for Chrome to establish a session (slow in a microVM)…")
    base_token = wait_browser(base_uds)
    if not base_token:
        _, dbg = bstate(base_uds)
        base.kill()
        raise SystemExit(f"Chrome never published a token: {dbg}")
    _, base_state = bstate(base_uds)
    print(f"[base] live session token={base_token}  state={base_state}")

    base.pause()
    base.snapshot(snap, mem, diff=False)
    base.kill()

    # concurrent fork with per-fork vsock remap
    children, uds_paths = [], []
    for i in range(args.forks):
        d = os.path.join(work, f"fork_{i}")
        uds_paths.append(os.path.join(d, "vm.vsock"))
        c = fc.Microvm(spec, work, name=f"child_{i}",
                       console=os.path.join(work, f"child_{i}.console"), vsock_remap=(BAKED, d))
        c._spawn()
        c.load(snap, mem, backend="File", resume=True)
        children.append(c)

    print(f"\n=== Spike J — browser fidelity across {args.forks}-way fork ===")
    print(f"  base live token: {base_token}\n")
    all_ok = True
    for i, uds in enumerate(uds_paths):
        wait_agent(uds, timeout=20)
        tok1, s1 = bstate(uds)
        time.sleep(1.0)
        tok2, s2 = bstate(uds)
        beat1 = s1.get("beat") if isinstance(s1, dict) else None
        beat2 = s2.get("beat") if isinstance(s2, dict) else None
        heap_ok = (tok1 == base_token)
        alive = (beat1 and beat2 and beat2.isdigit() and beat1.isdigit() and int(beat2) > int(beat1))
        ls_ok = isinstance(s2, dict) and s2.get("ls") == base_token
        ok = heap_ok and alive and ls_ok
        all_ok = all_ok and ok
        print(f"  fork {i}: token={tok1} heap_survived={heap_ok} alive(beat {beat1}->{beat2})={alive} "
              f"localStorage_ok={ls_ok}  [{'PASS' if ok else 'FAIL'}]")

    for c in children:
        c.kill()
    print(f"\n  VERDICT: {'BROWSER FIDELITY OK' if all_ok else 'ISSUE'} — "
          f"{'live Chrome session (JS heap + storage) survived fork, browser responsive' if all_ok else 'see failures'}")


if __name__ == "__main__":
    main()
