#!/usr/bin/env python3
"""Smoke test: prove Firecracker can boot + snapshot one microVM on this host.

Runs before the real spikes to de-risk "does Firecracker work under WSL2 nested KVM
at all." Uses a private writable copy of the rootfs so the pristine base is untouched.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time

import fc

KERNEL = os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux")
BASE_ROOTFS = os.environ.get("KLADOS_ROOTFS", "/opt/klados/assets/rootfs.ext4")


def main():
    for p in (KERNEL, BASE_ROOTFS):
        if not os.path.exists(p):
            raise SystemExit(f"missing asset: {p}")

    work = tempfile.mkdtemp(prefix="klados-smoke-")
    rootfs = os.path.join(work, "rootfs.ext4")
    print(f"copying rootfs -> {rootfs}")
    shutil.copy(BASE_ROOTFS, rootfs)

    spec = fc.VmSpec(kernel=KERNEL, rootfs=rootfs, mem_mib=1024, vcpus=1)
    with fc.Microvm(spec, work, name="smoke") as vm:
        t0 = time.perf_counter()
        vm.configure_and_start()
        boot_ms = (time.perf_counter() - t0) * 1000.0
        time.sleep(2.0)

        info = vm.api.get("/")
        print(f"instance info: {info}")
        state = info.get("state")

        snap = os.path.join(work, "s.snapshot")
        mem = os.path.join(work, "s.mem")
        t1 = time.perf_counter()
        vm.pause()
        vm.snapshot(snap, mem)
        pause_ms = (time.perf_counter() - t1) * 1000.0
        vm.resume()

        print(f"\nboot_to_api_ms = {boot_ms:.0f}")
        print(f"state          = {state}")
        print(f"snapshot pause_ms = {pause_ms:.0f}  (nested — not a real perf number)")
        print(f"snapshot file  = {os.path.getsize(snap)} bytes")
        print(f"mem file       = {os.path.getsize(mem)} bytes")
        print("\nSMOKE OK" if state == "Running" else f"\nSMOKE WARN: state={state}")


if __name__ == "__main__":
    main()
