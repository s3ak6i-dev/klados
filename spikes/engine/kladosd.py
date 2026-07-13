#!/usr/bin/env python3
"""kladosd — the Klados engine daemon (M0, Python reference implementation).

Owns the timeline DAG (SQLite) and the live instances, wrapping the primitives proven in the
spikes: boot, snapshot, and CONCURRENT copy-on-write fork with per-fork device remapping and
the zero-window fork protocol (entropy reseed + clock step + branch context).

A Run owns a Timeline (a DAG of Snapshots). An Instance is a live VM with a lineage pointer.
Every VM runs in its own mount namespace so the snapshot-baked vsock path resolves per-instance
(this is what makes concurrent fork work — see spike_i).

Run as root (mount namespaces + /dev/kvm):
    sudo python3 kladosd.py serve
API: HTTP/JSON on 127.0.0.1:7070. Production would be a Rust daemon over gRPC (PRD).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "harness"))
import fc  # noqa: E402

HOME = os.environ.get("KLADOS_HOME", "/var/lib/klados")
DB = os.path.join(HOME, "klados.db")
SNAPDIR = os.path.join(HOME, "snapshots")
RUNDIR = os.path.join(HOME, "instances")
BAKED = "/klados/vsock"  # fixed path baked into every snapshot; remapped per-instance

IMAGE = {
    "kernel": os.environ.get("KLADOS_KERNEL", "/opt/klados/assets/vmlinux"),
    "rootfs": os.environ.get("KLADOS_ROOTFS", "/opt/klados/assets/rootfs-entropy.ext4"),
    "init": "/init_protocol",
    "mem_mib": 512,
}

_id = lambda: uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- store
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(SNAPDIR, exist_ok=True)
    os.makedirs(RUNDIR, exist_ok=True)
    con = db()
    con.executescript("""
      CREATE TABLE IF NOT EXISTS runs(
        id TEXT PRIMARY KEY, image TEXT, created_at REAL);
      CREATE TABLE IF NOT EXISTS snapshots(
        id TEXT PRIMARY KEY, run_id TEXT, parent_id TEXT, label TEXT,
        snap_path TEXT, mem_path TEXT, size_bytes INTEGER, created_at REAL);
      CREATE TABLE IF NOT EXISTS instances(
        id TEXT PRIMARY KEY, run_id TEXT, snapshot_id TEXT, state TEXT,
        branch TEXT, created_at REAL);
    """)
    con.commit()
    con.close()


# ---------------------------------------------------------------- engine
LIVE: dict[str, fc.Microvm] = {}  # instance_id -> Microvm (in-memory; VMs die with the daemon)


SCRATCH_MIB = 32  # per-instance writable /data disk


def _spec():
    return fc.VmSpec(kernel=IMAGE["kernel"], rootfs=IMAGE["rootfs"], mem_mib=IMAGE["mem_mib"],
                     vcpus=1, track_dirty=True, rootfs_read_only=True,
                     vsock_uds=BAKED + "/vm.vsock", scratch=BAKED + "/scratch.ext4",
                     boot_args=f"console=ttyS0 reboot=k panic=1 pci=off init={IMAGE['init']}")


def _make_scratch(path):
    subprocess.run(["dd", "if=/dev/zero", f"of={path}", "bs=1M", f"count={SCRATCH_MIB}", "status=none"], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", path], check=True)


def _scratch_of(snapshot_id):
    return os.path.join(SNAPDIR, snapshot_id, "scratch.ext4")


def _walk_fs(root):
    """Return {relpath: (size, sha1)} for regular files, skipping lost+found."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root)
            if rel.startswith("lost+found"):
                continue
            try:
                h = hashlib.sha1()
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                out[rel] = [os.path.getsize(fp), h.hexdigest()[:12]]
            except OSError:
                pass
    return out


def _mount_ro(img):
    # norecovery: the layer was sealed while the guest had it mounted, so the ext4 journal is
    # "dirty"; a plain read-only mount refuses. norecovery mounts it without replaying the journal
    # (the committed data — synced on QUIESCE — is all present).
    mnt = tempfile.mkdtemp(prefix="klados-diff-")
    subprocess.run(["mount", "-o", "loop,ro,norecovery", img, mnt], check=True)
    return mnt


def _umount(mnt):
    subprocess.run(["umount", mnt], check=False)
    try:
        os.rmdir(mnt)
    except OSError:
        pass


def _instance_dir(iid):
    d = os.path.join(RUNDIR, iid)
    os.makedirs(d, exist_ok=True)
    return d


def _wait_agent(uds, timeout=20.0):
    """Poll the guest agent until it answers PING (it needs time to come up / resume)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fc.vsock_request(uds, 5000, {"type": "PING"}).get("status") == "PONG":
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _boot_vm(iid, load_from=None, scratch_src=None):
    """Boot a VM for instance iid. Fresh genesis if load_from is None, else restore a snapshot.
    scratch_src: seal a parent snapshot's disk layer to inherit it; else a fresh empty /data disk."""
    d = _instance_dir(iid)
    scratch_path = os.path.join(d, "scratch.ext4")
    if scratch_src and os.path.exists(scratch_src):
        shutil.copy(scratch_src, scratch_path)   # inherit parent's disk layer (CoW-by-copy for now)
    elif not os.path.exists(scratch_path):
        _make_scratch(scratch_path)
    vm = fc.Microvm(_spec(), d, name="vm", console=os.path.join(d, "console"),
                    vsock_remap=(BAKED, d))
    vm._spawn()
    if load_from is None:
        vm.configure_and_start()
    else:
        snap, mem = load_from
        vm.load(snap, mem, backend="File", resume=True)
    LIVE[iid] = vm
    return vm, os.path.join(d, "vm.vsock")


def create_run(image_label="klados/agent"):
    rid, iid = _id(), _id()
    con = db()
    con.execute("INSERT INTO runs VALUES(?,?,?)", (rid, image_label, time.time()))
    con.execute("INSERT INTO instances VALUES(?,?,?,?,?,?)",
                (iid, rid, None, "RUNNING", "genesis", time.time()))
    con.commit()
    con.close()
    _vm, uds = _boot_vm(iid)
    _wait_agent(uds)  # block until the guest agent is actually listening
    return {"run_id": rid, "instance_id": iid}


def snapshot_instance(iid, label="snap"):
    vm = LIVE.get(iid)
    if not vm:
        raise KeyError("no live instance " + iid)
    con = db()
    row = con.execute("SELECT run_id, snapshot_id FROM instances WHERE id=?", (iid,)).fetchone()
    run_id, parent = row["run_id"], row["snapshot_id"]
    sid = _id()
    sd = os.path.join(SNAPDIR, sid)
    os.makedirs(sd, exist_ok=True)
    snap, mem = os.path.join(sd, "vmstate"), os.path.join(sd, "mem")
    uds = os.path.join(_instance_dir(iid), "vm.vsock")
    try:
        fc.vsock_request(uds, 5000, {"type": "QUIESCE"})  # zero-window: stop workload before snapshot
        time.sleep(0.15)
    except Exception:
        pass
    vm.pause()
    vm.snapshot(snap, mem, diff=False)
    # seal the writable /data disk layer into the snapshot (guest synced it on QUIESCE)
    src_scratch = os.path.join(_instance_dir(iid), "scratch.ext4")
    if os.path.exists(src_scratch):
        shutil.copy(src_scratch, os.path.join(sd, "scratch.ext4"))
    vm.resume()
    size = os.stat(mem).st_blocks * 512 + os.path.getsize(snap)
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)",
                (sid, run_id, parent, label, snap, mem, size, time.time()))
    con.execute("UPDATE instances SET snapshot_id=? WHERE id=?", (sid, iid))
    con.commit()
    con.close()
    return {"snapshot_id": sid, "size_bytes": size, "parent": parent}


def fork_snapshot(sid, n=4):
    con = db()
    snap = con.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    if not snap:
        raise KeyError("no snapshot " + sid)
    run_id = snap["run_id"]
    children = []
    for i in range(n):
        iid = _id()
        vm, uds = _boot_vm(iid, load_from=(snap["snap_path"], snap["mem_path"]),
                           scratch_src=_scratch_of(sid))  # inherit the forked snapshot's disk layer
        branch = f"branch {i} of {n}"
        try:
            _wait_agent(uds)  # let the restored agent resume its accept loop
            r = fc.vsock_request(uds, 5000, {"type": "FORKED", "index": i, "n": n,
                                             "true_time": time.time(), "branch_context": branch})
            if r.get("status") != "READY":
                branch = "ERR:" + str(r)
        except Exception as e:
            branch = "ERR:" + str(e)
        # each fork is a new instance whose lineage points at the forked snapshot
        con.execute("INSERT INTO instances VALUES(?,?,?,?,?,?)",
                    (iid, run_id, sid, "RUNNING", branch, time.time()))
        children.append({"instance_id": iid, "branch": branch})
    con.commit()
    con.close()
    return {"children": children}


def fs_diff(sid_a, sid_b):
    """Filesystem diff between two snapshots' sealed /data layers (added/removed/modified files)."""
    a, b = _scratch_of(sid_a), _scratch_of(sid_b)
    if not (os.path.exists(a) and os.path.exists(b)):
        return {"error": "one or both snapshots have no sealed disk layer"}
    ma = _mount_ro(a)
    mb = _mount_ro(b)
    try:
        fa, fb = _walk_fs(ma), _walk_fs(mb)
        return {
            "a": sid_a, "b": sid_b,
            "added": sorted(p for p in fb if p not in fa),
            "removed": sorted(p for p in fa if p not in fb),
            "modified": sorted(p for p in fa if p in fb and fa[p] != fb[p]),
        }
    finally:
        _umount(ma)
        _umount(mb)


def destroy_instance(iid):
    vm = LIVE.pop(iid, None)
    if vm:
        vm.kill()
    con = db()
    con.execute("UPDATE instances SET state='DESTROYED' WHERE id=?", (iid,))
    con.commit()
    con.close()
    return {"instance_id": iid, "state": "DESTROYED"}


def timeline(run_id):
    con = db()
    snaps = [dict(r) for r in con.execute(
        "SELECT id,parent_id,label,size_bytes,created_at FROM snapshots WHERE run_id=? ORDER BY created_at", (run_id,))]
    insts = [dict(r) for r in con.execute(
        "SELECT id,snapshot_id,state,branch FROM instances WHERE run_id=?", (run_id,))]
    con.close()
    return {"run_id": run_id, "snapshots": snaps, "instances": insts}


def list_runs():
    con = db()
    runs = [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY created_at").fetchall()]
    con.close()
    return {"runs": runs}


# ---------------------------------------------------------------- HTTP API
ROUTES = []  # (method, regex-ish prefix, handler)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = self.path.strip("/").split("/")
        try:
            if p == ["v1", "runs"]:
                return self._send(200, list_runs())
            if len(p) == 4 and p[0] == "v1" and p[1] == "runs" and p[3] == "timeline":
                return self._send(200, timeline(p[2]))
            if len(p) == 5 and p[1] == "snapshots" and p[3] == "diff":
                return self._send(200, fs_diff(p[2], p[4]))
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        p = self.path.strip("/").split("/")
        try:
            b = self._body()
            if p == ["v1", "runs"]:
                return self._send(200, create_run(b.get("image", "klados/agent")))
            if len(p) == 4 and p[1] == "instances" and p[3] == "snapshot":
                return self._send(200, snapshot_instance(p[2], b.get("label", "snap")))
            if len(p) == 4 and p[1] == "snapshots" and p[3] == "fork":
                return self._send(200, fork_snapshot(p[2], int(b.get("n", 4))))
            if len(p) == 4 and p[1] == "instances" and p[3] == "destroy":
                return self._send(200, destroy_instance(p[2]))
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


def serve(host="127.0.0.1", port=7070):
    init_db()
    print(f"kladosd listening on http://{host}:{port}  (KLADOS_HOME={HOME})")
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        print("usage: kladosd.py serve")
