"""Minimal Firecracker client: HTTP over a unix domain socket, dependency-free.

Covers only what the A/B spikes need: boot config, start, pause/resume,
snapshot create (full/diff), snapshot load (File and Uffd backends).

API shape follows the Firecracker OpenAPI. Verify field names against the
firecracker version you install (see setup/provision.sh FC_VERSION) — the
snapshot/load bodies in particular have shifted across releases. TODO(host).
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


class FcApi:
    """Thin request wrapper around one firecracker api socket."""

    def __init__(self, sock_path: str):
        self.sock_path = sock_path

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = _UnixConn(self.sock_path)
        payload = json.dumps(body) if body is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status}: {raw.decode(errors='replace')}")
        return json.loads(raw) if raw else {}

    def put(self, path, body):   return self._req("PUT", path, body)
    def patch(self, path, body): return self._req("PATCH", path, body)
    def get(self, path):         return self._req("GET", path)


@dataclass
class VmSpec:
    kernel: str
    rootfs: str
    mem_mib: int = 2048
    vcpus: int = 2
    track_dirty: bool = True
    rootfs_read_only: bool = False
    scratch: str | None = None     # optional per-instance writable data disk (guest /dev/vdb)
    vsock_uds: str | None = None   # host-side unix socket for the guest vsock device
    vsock_cid: int = 3
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off i8042.noaux i8042.nomux"


class Microvm:
    """Launches a firecracker process and drives it. Use as a context manager."""

    def __init__(self, spec: VmSpec, workdir: str, name: str = "vm", console: str | None = None,
                 vsock_remap: tuple | None = None):
        self.spec = spec
        self.workdir = workdir
        self.name = name
        self.console = console  # if set, firecracker stdout/stderr (serial console) -> this file
        # vsock_remap = (baked_dir, perfork_dir): launch firecracker in a private mount namespace
        # where baked_dir (the path baked into the snapshot) is bind-mounted to perfork_dir, so
        # each fork's baked vsock socket resolves to its own file. This is the jailer-lite trick
        # that makes CONCURRENT N-way fork work despite Firecracker baking host paths in snapshots.
        self.vsock_remap = vsock_remap
        self.sock = os.path.join(workdir, f"{name}.sock")
        self.proc: subprocess.Popen | None = None
        self._console_fh = None
        self.api: FcApi | None = None
        os.makedirs(workdir, exist_ok=True)

    def __enter__(self) -> "Microvm":
        self._spawn()
        return self

    def __exit__(self, *exc):
        self.kill()

    def _spawn(self):
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        if self.console:
            self._console_fh = open(self.console, "wb")
            out = err = self._console_fh
        else:
            out = err = subprocess.DEVNULL
        if self.vsock_remap:
            baked, perfork = self.vsock_remap
            os.makedirs(perfork, exist_ok=True)
            inner = (f"mkdir -p '{baked}' && mount --bind '{perfork}' '{baked}' && "
                     f"exec firecracker --api-sock '{self.sock}'")
            cmd = ["unshare", "--mount", "--propagation", "private", "sh", "-c", inner]
        else:
            cmd = ["firecracker", "--api-sock", self.sock]
        self.proc = subprocess.Popen(cmd, stdout=out, stderr=err)
        self.api = FcApi(self.sock)
        self._await_sock()

    def _await_sock(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.sock):
                try:
                    self.api.get("/")  # any call proves the API is up
                    return
                except Exception:
                    pass
            time.sleep(0.01)
        raise TimeoutError(f"firecracker api socket {self.sock} never came up")

    # --- boot a fresh VM from an image ---
    def configure_and_start(self):
        s = self.spec
        self.api.put("/boot-source", {"kernel_image_path": s.kernel, "boot_args": s.boot_args})
        self.api.put("/drives/rootfs", {
            "drive_id": "rootfs", "path_on_host": s.rootfs,
            "is_root_device": True, "is_read_only": s.rootfs_read_only,
        })
        if s.scratch:
            self.api.put("/drives/scratch", {
                "drive_id": "scratch", "path_on_host": s.scratch,
                "is_root_device": False, "is_read_only": False,
            })
        self.api.put("/machine-config", {
            "vcpu_count": s.vcpus, "mem_size_mib": s.mem_mib,
            "track_dirty_pages": s.track_dirty,
        })
        if s.vsock_uds:
            self.api.put("/vsock", {"guest_cid": s.vsock_cid, "uds_path": s.vsock_uds})
        self.api.put("/actions", {"action_type": "InstanceStart"})

    # --- snapshot / restore ---
    def pause(self):  self.api.patch("/vm", {"state": "Paused"})
    def resume(self): self.api.patch("/vm", {"state": "Resumed"})

    def snapshot(self, snap_path: str, mem_path: str, diff: bool = False):
        self.api.put("/snapshot/create", {
            "snapshot_type": "Diff" if diff else "Full",
            "snapshot_path": snap_path,
            "mem_file_path": mem_path,
        })

    def load(self, snap_path: str, mem_path: str, *, backend: str = "File",
             resume: bool = True, enable_diff: bool = False, vsock_uds: str | None = None):
        """backend: 'File' (mmap the mem file) or 'Uffd' (mem_path is the handler's uds).

        vsock_uds: give this restored VM its own host-side vsock socket (each fork needs a
        distinct path). Forces load-without-resume, overrides /vsock, then resumes.
        """
        if backend == "Uffd":
            mem_backend = {"backend_type": "Uffd", "backend_path": mem_path}
        else:
            mem_backend = {"backend_type": "File", "backend_path": mem_path}
        do_resume = resume and vsock_uds is None
        self.api.put("/snapshot/load", {
            "snapshot_path": snap_path,
            "mem_backend": mem_backend,
            "enable_diff_snapshots": enable_diff,
            "resume_vm": do_resume,
        })
        if vsock_uds is not None:
            # per-fork vsock: override the snapshot's uds, then resume.
            self.api.put("/vsock", {"guest_cid": self.spec.vsock_cid, "uds_path": vsock_uds})
            if resume:
                self.resume()

    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            try:
                self.proc.wait(timeout=2)
            except Exception:
                pass
        if self._console_fh:
            try:
                self._console_fh.close()
            except Exception:
                pass
            self._console_fh = None
        for p in (self.sock,):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def vsock_request(uds_path: str, port: int, payload: dict, timeout: float = 5.0) -> dict:
    """Host->guest vsock request via Firecracker's hybrid vsock. Connect to the host uds,
    send the 'CONNECT <port>' handshake, then a JSON line; return the JSON reply."""
    import json as _json
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(uds_path)
    s.sendall(b"CONNECT %d\n" % port)
    buf = b""
    while b"\n" not in buf:
        d = s.recv(64)
        if not d:
            raise RuntimeError("vsock: no CONNECT ack (guest not listening on port?)")
        buf += d
    if not buf.startswith(b"OK"):
        raise RuntimeError("vsock CONNECT rejected: %r" % buf)
    s.sendall((_json.dumps(payload) + "\n").encode())
    resp = b""
    while b"\n" not in resp:
        d = s.recv(1024)
        if not d:
            break
        resp += d
    s.close()
    line = resp.split(b"\n", 1)[0].decode("utf-8", "ignore")
    return _json.loads(line) if line else {}
