"""Stats + memory accounting for the spikes. Dependency-free."""
from __future__ import annotations

import json
import os


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "min": round(min(values), 2) if values else None,
        "p50": round(percentile(values, 50), 2),
        "p99": round(percentile(values, 99), 2),
        "max": round(max(values), 2) if values else None,
        "mean": round(sum(values) / len(values), 2) if values else None,
    }


def pss_kib(pid: int) -> int:
    """Proportional set size in KiB from smaps_rollup — shared pages charged once.

    This is the honest metric for fork economics: a base page shared by N children
    contributes base/N to each child's PSS, so Σ PSS across children ≈ base + divergence.
    Falls back to VmRSS (which double-counts shared pages) if smaps_rollup is unavailable.
    """
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def print_table(title: str, rows: list[tuple[str, str]]):
    print(f"\n=== {title} ===")
    w = max((len(k) for k, _ in rows), default=0)
    for k, v in rows:
        print(f"  {k.ljust(w)} : {v}")


def save(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)
    print(f"\nwrote {path}")
