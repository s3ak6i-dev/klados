#!/usr/bin/env python3
"""Fidelity workload (Spike H): a multi-process program with live in-memory state and a
continuously-written on-disk SQLite database. Represents the hard part of browser fidelity —
multiple processes, threads, and a DB being mutated at the instant of snapshot.

- parent holds an in-memory session token and spawns N worker PROCESSES
- each worker writes rows to a shared SQLite DB (WAL) in a tight loop and commits
- snapshotting happens WHILE these writes are in flight (no quiesce) to test crash-consistency
"""
import os
import secrets
import sqlite3
import sys
import time
from multiprocessing import Process

DB = "/run/klados/app.db"
N_WORKERS = 3


def worker(wid: int):
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    seq = 0
    while True:
        con.execute("INSERT INTO writes(worker, seq, token) VALUES (?,?,?)",
                    (wid, seq, secrets.token_hex(8)))
        con.commit()
        seq += 1
        time.sleep(0.01)


def main():
    os.makedirs("/run/klados", exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS writes(worker INT, seq INT, token TEXT)")
    con.commit()
    con.close()

    session = secrets.token_hex(16)  # in-memory session identity
    with open("/run/klados/session", "w") as f:
        f.write(session)
    sys.stdout.write("FIDELITY session=%s workers=%d\n" % (session, N_WORKERS))
    sys.stdout.flush()

    procs = [Process(target=worker, args=(w,)) for w in range(N_WORKERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
