#!/usr/bin/env python3
"""klados-guest-agent (spike edition) — supervisor model for zero-window fork safety.

The agent is PID 1. It SPAWNS the workload as a child so it can gate execution:

  QUIESCE  (sent before snapshot) -> SIGSTOP the workload. The snapshot therefore captures
           the workload STOPPED, so no forked child can run workload code before the agent
           has reseeded.
  FORKED {index,n,true_time,branch_context} (sent after each restore) ->
           inject branch context, step CLOCK_REALTIME, bump /run/klados/forkgen, THEN SIGCONT
           the workload. The workload's first post-fork RNG call runs only after the generation
           has changed, so the event-mode hook reseeds before producing any value -> zero window.
  PING -> PONG.

Production would be a static Rust binary (PRD D5); Python here because it's in the guest.
"""
import ctypes
import ctypes.util
import json
import os
import signal
import socket
import subprocess
import sys
import time

PORT = 5000
RUN = "/run/klados"
GEN = RUN + "/forkgen"
BRANCH = RUN + "/branch.json"
CID_ANY = getattr(socket, "VMADDR_CID_ANY", 0xFFFFFFFF)

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


class _timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def set_realtime(epoch: float) -> bool:
    ts = _timespec(int(epoch), int((epoch - int(epoch)) * 1e9))
    return _libc.clock_settime(0, ctypes.byref(ts)) == 0


def _write_atomic(path: str, data: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def _bump_gen() -> int:
    try:
        cur = int(open(GEN).read().strip() or 0)
    except Exception:
        cur = 0
    _write_atomic(GEN, str(cur + 1))
    return cur + 1


class Agent:
    def __init__(self, workload_argv):
        os.makedirs(RUN, exist_ok=True)
        _write_atomic(GEN, "0")
        env = dict(os.environ, PYTHONPATH="/klados", KLADOS_RESEED="event")
        self.work = subprocess.Popen(workload_argv, env=env)

    def handle(self, msg: dict) -> dict:
        t = msg.get("type")
        if t == "PING":
            return {"status": "PONG"}
        if t == "CHECK":
            # inspect the workload's on-disk SQLite DB (fidelity spike): integrity + per-worker rows
            import sqlite3
            db = msg.get("db", "/run/klados/app.db")
            out = {"status": "CHECK"}
            try:
                con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
                out["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
                rows = con.execute("SELECT worker, COUNT(*) FROM writes GROUP BY worker").fetchall()
                out["per_worker"] = {str(w): c for w, c in rows}
                out["total"] = con.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
                try:
                    out["session"] = open("/run/klados/session").read().strip()
                except Exception:
                    out["session"] = None
                con.close()
            except Exception as e:
                out["error"] = str(e)
            return out
        if t == "QUIESCE":
            self.work.send_signal(signal.SIGSTOP)
            return {"status": "QUIESCED", "pid": self.work.pid}
        if t == "BROWSER_CHECK":
            def rd(p):
                try:
                    return open(p).read().strip()
                except Exception:
                    return None
            return {"status": "BROWSER", "token": rd("/run/klados/browser_token"),
                    "state": rd("/run/klados/browser_state")}
        if t == "FORKED":
            idx, ctx = msg.get("index"), msg.get("branch_context")
            clock_set = None
            if ctx is not None:
                _write_atomic(BRANCH, json.dumps({"index": idx, "n": msg.get("n"), "context": ctx}))
            if "true_time" in msg and set_realtime(float(msg["true_time"])):
                clock_set = time.time()
            gen = _bump_gen()                      # bump generation FIRST ...
            self.work.send_signal(signal.SIGCONT)  # ... then release the workload
            sys.stdout.write("KLADOS_FORKED index=%s gen=%s clock=%.3f context=%s\n"
                             % (idx, gen, clock_set or -1, ctx))
            sys.stdout.flush()
            return {"status": "READY", "index": idx, "gen": gen, "clock": clock_set}
        return {"status": "ERR", "msg": "unknown type %r" % t}


def main():
    workload = sys.argv[1:] or ["/usr/bin/python3", "-u", "/entropy_gen.py"]
    agent = Agent(workload)
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((CID_ANY, PORT))
    s.listen(8)
    sys.stdout.write("KLADOS_AGENT listening vsock:%d workload=%s\n" % (PORT, workload))
    sys.stdout.flush()
    while True:
        try:
            conn, _ = s.accept()
        except Exception:
            continue
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\n", 1)[0].decode("utf-8", "ignore")
            resp = agent.handle(json.loads(line)) if line else {"status": "ERR"}
            conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"status": "ERR", "msg": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()


if __name__ == "__main__":
    main()
