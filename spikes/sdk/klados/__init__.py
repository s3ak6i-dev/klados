"""Klados Python SDK (M1 alpha).

Thin, async-free client for the kladosd engine. Mirrors the north-star ergonomics from the PRD:

    from klados import Sandbox

    sb = Sandbox.create(image="klados/agent-base:py3.12")
    snap = sb.snapshot(label="before-risky-refactor")
    children = snap.fork(4)
    ...
    for c in children:
        c.destroy()

Set KLADOS_API to point at a non-default daemon (default http://127.0.0.1:7070).
"""
from __future__ import annotations

import json
import os
import urllib.request

__all__ = ["Sandbox", "Snapshot", "Timeline", "KladosError"]

_API = os.environ.get("KLADOS_API", "http://127.0.0.1:7070")


class KladosError(RuntimeError):
    pass


def _call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise KladosError(f"{method} {path}: {e.read().decode()}") from None
    except urllib.error.URLError as e:
        raise KladosError(f"cannot reach kladosd at {_API} ({e}). Is the daemon running?") from None


class Timeline:
    """The snapshot DAG of a run (nodes + attached instances)."""

    def __init__(self, data: dict):
        self._d = data

    @property
    def run_id(self) -> str:
        return self._d["run_id"]

    @property
    def snapshots(self) -> list[dict]:
        return self._d["snapshots"]

    @property
    def instances(self) -> list[dict]:
        return self._d["instances"]

    def __repr__(self):
        return f"Timeline(run={self.run_id}, snapshots={len(self.snapshots)}, instances={len(self.instances)})"


class Snapshot:
    """An immutable node in a run's timeline. Forkable and restorable."""

    def __init__(self, snapshot_id: str, run_id: str):
        self.id = snapshot_id
        self.run_id = run_id

    def fork(self, n: int = 1) -> list["Sandbox"]:
        """Create N copy-on-write children that diverge from this instant."""
        r = _call("POST", f"/v1/snapshots/{self.id}/fork", {"n": n})
        return [Sandbox(c["instance_id"], self.run_id, branch=c.get("branch")) for c in r["children"]]

    def __repr__(self):
        return f"Snapshot({self.id})"


class Sandbox:
    """A live agent instance (a running microVM) with a lineage in its run's timeline."""

    def __init__(self, instance_id: str, run_id: str, branch: str | None = None):
        self.instance_id = instance_id
        self.run_id = run_id
        self.branch = branch

    @classmethod
    def create(cls, image: str = "klados/agent") -> "Sandbox":
        r = _call("POST", "/v1/runs", {"image": image})
        return cls(r["instance_id"], r["run_id"], branch="genesis")

    def snapshot(self, label: str = "snap") -> Snapshot:
        r = _call("POST", f"/v1/instances/{self.instance_id}/snapshot", {"label": label})
        return Snapshot(r["snapshot_id"], self.run_id)

    def timeline(self) -> Timeline:
        return Timeline(_call("GET", f"/v1/runs/{self.run_id}/timeline"))

    def destroy(self) -> None:
        _call("POST", f"/v1/instances/{self.instance_id}/destroy", {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.destroy()
        except KladosError:
            pass

    def __repr__(self):
        return f"Sandbox({self.instance_id}, branch={self.branch!r})"
